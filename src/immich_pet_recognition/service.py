import hashlib
import io
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import numpy as np
from PIL import Image, ImageOps

from .config import Settings
from .grouping import similar_groups
from .immich import ImmichClient, ImmichError, asset_revision
from .learning import PROFILES, fit_policies
from .matching import match_pet
from .models import NoodleRecognizer, RFDetrDetector
from .store import Store

logger = logging.getLogger(__name__)


def eligible(asset):
    return (
        asset.get("type") == "IMAGE"
        and not asset.get("isTrashed")
        and not asset.get("isOffline")
        and asset.get("visibility", "timeline") in {"timeline", "archive"}
    )


class Service:
    def __init__(self, settings: Settings, immich=None, detector=None, recognizer=None):
        self.settings = settings
        self.store = Store(settings.data_dir / "pets.sqlite3")
        mode = "demo" if settings.demo else "real"
        existing_mode = self.store.state("data_mode")
        if existing_mode and existing_mode != mode:
            raise ValueError("Use separate PET_DATA_DIR locations for demo and real operation")
        self.store.set_state("data_mode", mode)
        self.crops = settings.data_dir / "crops"
        self.crops.mkdir(exist_ok=True)
        self.immich = immich or ImmichClient(
            settings.immich_url, settings.immich_api_key.get_secret_value()
        )
        self.detector = detector or RFDetrDetector(settings)
        self.recognizer = recognizer or NoodleRecognizer(settings)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.sync_requested = threading.Event()
        self.backfill_requested = threading.Event()
        self.rematch_requested = threading.Event()
        self.worker = None
        # Serializes GPU work and decisions so in-flight matching cannot overwrite human review.
        self.model_lock = threading.RLock()
        self.recheck_lock = threading.Lock()
        self.recheck = json.loads(self.store.state("last_recheck", "{}"))
        self.policy_lock = threading.RLock()
        saved_policy = json.loads(self.store.state("last_matching_policy", "null"))
        self.policy_cache = (
            (tuple(saved_policy["key"]), saved_policy["policies"]) if saved_policy else None
        )
        self.group_cache = None
        self.group_lock = threading.Lock()
        self.group_snapshots = {}
        self.review_generation = 0
        self.publish_lock = threading.Lock()
        self.active_publication = None
        self.inference_lock = threading.RLock()
        self.rematch_not_before = 0.0
        self.batch_depth = 0
        self.batch_gallery_dirty = False

    def start(self):
        self.store.execute("UPDATE jobs SET status='queued' WHERE status='processing'")
        self.adopt_manual_identifications()
        if self.settings.demo:
            from .demo import seed_demo

            seed_demo(self)
            if self.suggestions_pending():
                self.rematch()
        elif self.settings.background:
            if self.matching_options()["mode"] == "adaptive" and self.policy_cache is None:
                self.gallery_changed()
            if self.suggestions_pending():
                self.rematch_requested.set()
            self.worker = threading.Thread(target=self.run, name="pet-worker", daemon=True)
            self.worker.start()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.worker:
            self.worker.join(timeout=75)
        # Allow in-flight inference to finish. Interrupted jobs recover on the next start.
        if self.worker is None or not self.worker.is_alive():
            self.immich.close()

    def adopt_manual_identifications(self):
        """Older manual names are identity feedback too, without an enrollment step."""
        with self.model_lock:
            changed = self.store.execute(
                "DELETE FROM reference_images WHERE kind='positive' AND EXISTS("
                "SELECT 1 FROM reviews r JOIN detections d ON d.id=r.detection_id "
                "JOIN assets a ON a.id=d.asset_id "
                "WHERE r.detection_id=reference_images.detection_id "
                "AND r.status='confirmed' AND r.pet_id<>reference_images.pet_id AND r.model=? "
                "AND d.detector=? AND d.revision=a.revision AND NOT EXISTS("
                "SELECT 1 FROM automatic_reviews ar WHERE ar.detection_id=r.detection_id "
                "AND ar.model=r.model))",
                (self.settings.recognizer_key, self.settings.detector_key),
            )
            changed += self.store.execute(
                "INSERT INTO reference_images(pet_id,detection_id,kind) "
                "SELECT r.pet_id,r.detection_id,'positive' FROM reviews r "
                "JOIN detections d ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
                "WHERE r.status='confirmed' AND r.pet_id IS NOT NULL AND r.model=? "
                "AND d.detector=? AND d.revision=a.revision AND NOT EXISTS("
                "SELECT 1 FROM automatic_reviews ar WHERE ar.detection_id=r.detection_id "
                "AND ar.model=r.model) ON CONFLICT(pet_id,detection_id) "
                "DO UPDATE SET kind='positive' WHERE kind<>'positive'",
                (self.settings.recognizer_key, self.settings.detector_key),
            )
            if changed:
                self.gallery_changed()

    def run(self):
        next_poll = 0.0
        while not self.stop_event.is_set():
            try:
                if self.rematch_requested.is_set() and time.monotonic() >= self.rematch_not_before:
                    self.rematch_requested.clear()
                    self.rematch()
                if self.publish_one():
                    continue
                if self.sync_requested.is_set() or time.monotonic() >= next_poll:
                    self.sync_requested.clear()
                    full = self.backfill_requested.is_set()
                    self.backfill_requested.clear()
                    self.sync(full=full)
                    next_poll = time.monotonic() + self.settings.poll_seconds
                if self.process_one():
                    continue
            except Exception as error:
                self.store.set_state("last_error", self.safe_error(error))
                logger.exception("Background task failed")
                next_poll = time.monotonic() + self.settings.poll_seconds
            self.wake_event.wait(2)
            self.wake_event.clear()

    @staticmethod
    def safe_error(error):
        if isinstance(error, (ImmichError, ValueError, RuntimeError)):
            return str(error)[:300]
        return f"{type(error).__name__}; check the service logs and configuration"

    def sync(self, full=False):
        if self.settings.demo:
            return 0
        started = datetime.now(UTC)
        since = None if full else self.store.state("sync_watermark")
        if since:
            since = (datetime.fromisoformat(since) - timedelta(seconds=60)).isoformat()
        count = 0
        for asset in self.immich.assets(since):
            if self.stop_event.is_set():
                return count
            if eligible(asset):
                # Only store fields needed for revision and processing, excluding paths/EXIF.
                metadata = {
                    k: asset[k]
                    for k in (
                        "id",
                        "checksum",
                        "fileModifiedAt",
                        "thumbhash",
                        "originalFileName",
                        "type",
                        "visibility",
                        "isTrashed",
                        "isOffline",
                    )
                    if k in asset
                }
                count += self.store.enqueue(
                    metadata, asset_revision(asset), self.settings.pipeline_key
                )
        self.store.set_state("sync_watermark", started.isoformat())
        self.store.set_state("last_sync", started.isoformat())
        self.store.set_state("last_error", "")
        return count

    def process_one(self):
        if self.settings.demo:
            return False
        job = self.store.claim(self.settings.pipeline_key)
        if job is None:
            return False
        try:
            asset = self.immich.asset(job["asset_id"])
            if eligible(asset) and asset_revision(asset) == job["revision"]:
                self.process(job)
            elif eligible(asset):
                self.store.enqueue(asset, asset_revision(asset), self.settings.pipeline_key)
            self.store.execute(
                "UPDATE jobs SET status='completed',error=NULL WHERE id=?", (job["id"],)
            )
        except Exception as error:
            status = "failed" if job["attempts"] >= 3 else "queued"
            self.store.execute(
                "UPDATE jobs SET status=?,error=?,available_at=? WHERE id=?",
                (
                    status,
                    self.safe_error(error),
                    time.time() + 10 * 2 ** job["attempts"],
                    job["id"],
                ),
            )
            logger.exception("Asset processing failed")
        return True

    def process(self, job):
        key = (job["asset_id"], job["revision"], self.settings.detector_key)
        cached = self.store.one(
            "SELECT 1 FROM passes WHERE asset_id=? AND revision=? AND detector=?", key
        )
        if not cached:
            raw = self.immich.preview(job["asset_id"])
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
            with self.inference_lock:
                detections = self.detector.detect(image)
            entries = []
            for index, detection in enumerate(detections):
                digest = hashlib.sha256(("|".join(key) + f"|{index}").encode()).hexdigest()[:32]
                crop_name = digest + ".jpg"
                image.crop(detection.box).save(self.crops / crop_name, quality=95)
                entries.append(
                    (
                        digest,
                        *key,
                        detection.species,
                        detection.score,
                        json.dumps(detection.box),
                        crop_name,
                    )
                )
            with self.store.connection() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO detections VALUES (?,?,?,?,?,?,?,?)", entries
                )
                conn.execute("INSERT OR IGNORE INTO passes VALUES (?,?,?)", key)
        for detection in self.store.rows(
            "SELECT * FROM detections WHERE asset_id=? AND revision=? AND detector=?", key
        ):
            self.embed(detection)
            with self.model_lock:
                self.evaluate(detection)

    def embed(self, detection):
        vector = self.store.embedding(detection["id"], self.settings.recognizer_key)
        if vector is None:
            if self.settings.demo:
                raise ValueError("Demo embeddings are unavailable for this crop")
            with self.inference_lock:
                with Image.open(self.crops / detection["crop"]) as crop:
                    vector = self.recognizer.embed(crop)
            self.store.save_embedding(detection["id"], self.settings.recognizer_key, vector)
        return vector

    def references(self, cached_only=False):
        gallery = []
        for pet in self.store.rows("SELECT * FROM pets ORDER BY name"):
            pet["positive"], pet["negative"] = [], []
            for kind in ("positive", "negative"):
                pet[kind + "_ids"], pet[kind + "_assets"] = [], []
            for reference in self.store.rows(
                "SELECT d.*,r.kind,e.vector FROM reference_images r "
                "JOIN detections d ON d.id=r.detection_id "
                "LEFT JOIN embeddings e ON e.detection_id=d.id AND e.model=? "
                "WHERE r.pet_id=? ORDER BY d.id",
                (self.settings.recognizer_key, pet["id"]),
            ):
                if reference["vector"] is not None:
                    vector = np.frombuffer(reference["vector"], dtype=np.float32).copy()
                else:
                    vector = None if cached_only else self.embed(reference)
                if vector is not None:
                    pet[reference["kind"]].append(vector)
                    pet[reference["kind"] + "_ids"].append(reference["id"])
                    pet[reference["kind"] + "_assets"].append(reference["asset_id"])
            gallery.append(pet)
        return gallery

    def matching_options(self):
        return json.loads(
            self.store.state(
                "matching_options",
                json.dumps({"mode": self.settings.matching_mode, "profile": "balanced"}),
            )
        )

    def policies(self, gallery=None, mode=None, profile=None, refresh=False, revision=None):
        options = self.matching_options()
        mode, profile = mode or options["mode"], profile or options["profile"]
        key = (
            revision if revision is not None else self.store.state("gallery_revision", "0"),
            self.settings.recognizer_key,
            mode,
            profile,
            self.settings.match_threshold,
            self.settings.match_margin,
            self.settings.min_references,
        )
        with self.policy_lock:
            if self.policy_cache and self.policy_cache[0] == key:
                return self.policy_cache[1]
            # Reads and human decisions never fit a new gallery. The worker can
            # finish a fit while the page keeps using its last ready snapshot.
            if not refresh and self.policy_cache and self.policy_cache[0][1:] == key[1:]:
                return self.policy_cache[1]
        if mode == "adaptive" and refresh:
            gallery = self.references(cached_only=True) if gallery is None else gallery
            policies = fit_policies(
                gallery,
                self.settings.match_threshold,
                self.settings.match_margin,
                self.settings.min_references,
                profile,
            )
        else:
            gallery = self.store.rows("SELECT id FROM pets") if gallery is None else gallery
            policies = {
                pet["id"]: {
                    "threshold": min(self.settings.match_threshold, 0.75)
                    if mode == "adaptive" and profile != "careful"
                    else self.settings.match_threshold,
                    "margin": min(self.settings.match_margin, 0.03)
                    if mode == "adaptive" and profile != "careful"
                    else self.settings.match_margin,
                    "min_references": min(self.settings.min_references, 3)
                    if mode == "adaptive"
                    else self.settings.min_references,
                    "state": "starting" if mode == "adaptive" else "fixed",
                }
                for pet in gallery
            }
        if refresh or mode == "fixed":
            with self.model_lock:
                with self.policy_lock:
                    if key[0] != self.store.state("gallery_revision", "0"):
                        return policies
                    self.policy_cache = (key, policies)
                self.store.set_state(
                    "last_matching_policy", json.dumps({"key": key, "policies": policies})
                )
        return policies

    def matching_preview(self, mode=None, profile=None):
        options = self.matching_options()
        mode, profile = mode or options["mode"], profile or options["profile"]
        policies = self.policies(mode=mode, profile=profile)
        rows = self.store.rows(
            "SELECT p.id,p.name,count(d.id) eligible_crops,"
            "count(DISTINCT d.asset_id) eligible_photos FROM pets p "
            "LEFT JOIN json_each(?) rule ON rule.key=p.id "
            "LEFT JOIN reviews r ON r.pet_id=p.id AND r.model=? AND r.status='pending' "
            "AND r.score>=json_extract(rule.value,'$.threshold') "
            "AND r.margin>=json_extract(rule.value,'$.margin') "
            "AND (SELECT count(*) FROM reference_images refs WHERE refs.pet_id=p.id "
            "AND refs.kind='positive')>=json_extract(rule.value,'$.min_references') "
            "LEFT JOIN detections d ON d.id=r.detection_id AND d.detector=? "
            "AND EXISTS(SELECT 1 FROM assets a WHERE a.id=d.asset_id AND a.revision=d.revision) "
            "GROUP BY p.id ORDER BY p.name",
            (json.dumps(policies), self.settings.recognizer_key, self.settings.detector_key),
        )
        for row in rows:
            row["policy"] = policies.get(row["id"], {})
            row["enabled"] = self.automation_enabled(row["id"])
        return {
            "mode": mode,
            "profile": profile,
            "active": options,
            "pets": rows,
            "provisional": self.suggestions_pending(),
        }

    def set_matching_options(self, mode, profile):
        if mode not in {"adaptive", "fixed"} or profile not in PROFILES:
            raise ValueError("Choose a matching mode and sorting preference")
        with self.model_lock:
            self.store.set_state("matching_options", json.dumps({"mode": mode, "profile": profile}))
            self.gallery_changed()
        return self.matching_preview()

    def evaluate(self, detection, gallery=None):
        model = self.settings.recognizer_key
        existing = self.store.one(
            "SELECT * FROM reviews WHERE detection_id=? AND model=?", (detection["id"], model)
        )
        if existing and existing["status"] != "pending":
            return False
        gallery = self.references() if gallery is None else gallery
        result = match_pet(
            self.embed(detection),
            detection["species"],
            gallery,
            self.settings.match_threshold,
            self.settings.match_margin,
            self.settings.min_references,
            self.policies(gallery, refresh=True),
        )
        automatic = result.confident and self.automation_enabled(result.pet_id)
        status = "confirmed" if automatic else "pending"
        with self.store.connection() as conn:
            conn.execute(
                "INSERT INTO reviews(detection_id,model,pet_id,score,margin,status) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(detection_id,model) DO UPDATE SET "
                "pet_id=excluded.pet_id,score=excluded.score,"
                "margin=excluded.margin,status=excluded.status",
                (detection["id"], model, result.pet_id, result.score, result.margin, status),
            )
            self._review_changed(conn)
            if automatic:
                self.group_cache = None
                conn.execute(
                    "INSERT OR IGNORE INTO automatic_reviews VALUES (?,?)",
                    (detection["id"], model),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO automatic_sources VALUES (?,?,?)",
                    (detection["id"], model, self.store.state("gallery_revision", "0")),
                )
        return automatic

    def automation_enabled(self, pet_id):
        if not pet_id:
            return False
        row = self.store.one("SELECT enabled FROM pet_automation WHERE pet_id=?", (pet_id,))
        return bool(row["enabled"]) if row else self.settings.auto_tag

    def _review_changed(self, conn):
        # This only invalidates an in-memory cache; writing a durable counter
        # for every crop made each background pass thousands of tiny writes.
        self.review_generation += 1
        self.group_cache = None

    def pets(self):
        policies = self.policies()
        pets = self.store.rows(
            "SELECT p.*,count(CASE WHEN r.kind='positive' THEN 1 END) positive_count,"
            "count(CASE WHEN r.kind='negative' THEN 1 END) negative_count FROM pets p "
            "LEFT JOIN reference_images r ON p.id=r.pet_id GROUP BY p.id ORDER BY p.name"
        )
        counts = {
            row["pet_id"]: row
            for row in self.store.rows(
                "SELECT r.pet_id,"
                "sum(r.status='pending') pending_count,"
                "sum(r.status='pending' AND r.score>=json_extract(rule.value,'$.threshold') "
                "AND r.margin>=json_extract(rule.value,'$.margin')) strong_count,"
                "sum(r.status='confirmed' AND r.published=1) tagged_count,"
                "sum(r.status='confirmed' AND r.published=0) queued_count,"
                "sum(r.status='confirmed' AND r.published=1 AND ar.detection_id IS NOT NULL) "
                "auto_tagged_count FROM reviews r JOIN detections d ON d.id=r.detection_id "
                "JOIN assets a ON a.id=d.asset_id LEFT JOIN automatic_reviews ar "
                "ON ar.detection_id=r.detection_id AND ar.model=r.model "
                "LEFT JOIN json_each(?) rule ON rule.key=r.pet_id "
                "WHERE r.model=? AND d.detector=? AND d.revision=a.revision GROUP BY r.pet_id",
                (
                    json.dumps(policies),
                    self.settings.recognizer_key,
                    self.settings.detector_key,
                ),
            )
        }
        for pet in pets:
            pet.update(
                {
                    key: counts.get(pet["id"], {}).get(key, 0)
                    for key in (
                        "pending_count",
                        "strong_count",
                        "tagged_count",
                        "queued_count",
                        "auto_tagged_count",
                    )
                }
            )
            pet["auto_tag"] = self.automation_enabled(pet["id"])
            pet["policy"] = policies.get(pet["id"], {})
            pet["example_gate_met"] = pet["positive_count"] >= pet["policy"].get(
                "min_references", self.settings.min_references
            )
            if not pet["example_gate_met"]:
                pet["strong_count"] = 0
        return pets

    def set_automation(self, pet_id, enabled):
        with self.model_lock:
            if not self.store.one("SELECT id FROM pets WHERE id=?", (pet_id,)):
                raise ValueError("Pet not found")
            with self.store.connection() as conn:
                conn.execute(
                    "INSERT INTO pet_automation VALUES (?,?) ON CONFLICT(pet_id) "
                    "DO UPDATE SET enabled=excluded.enabled",
                    (pet_id, int(enabled)),
                )
                if not enabled:
                    # A network write may be in flight outside the human lock.
                    # Record its compensation before canceling the automatic name.
                    if self.active_publication:
                        conn.execute(
                            "INSERT OR IGNORE INTO tag_removals "
                            "SELECT r.detection_id,r.model,d.asset_id,p.tag FROM reviews r "
                            "JOIN detections d ON d.id=r.detection_id "
                            "JOIN pets p ON p.id=r.pet_id JOIN automatic_reviews ar "
                            "ON ar.detection_id=r.detection_id AND ar.model=r.model "
                            "WHERE r.pet_id=? AND r.model=? AND r.status='confirmed' "
                            "AND r.published=0 AND r.detection_id=?",
                            (
                                pet_id,
                                self.settings.recognizer_key,
                                self.active_publication["detection_id"],
                            ),
                        )
                    # Pause also cancels automatic writes that have not reached Immich.
                    conn.execute(
                        "UPDATE reviews SET status='pending',error=NULL WHERE pet_id=? "
                        "AND published=0 AND status='confirmed' AND model=? "
                        "AND EXISTS (SELECT 1 FROM automatic_reviews ar WHERE "
                        "ar.detection_id=reviews.detection_id AND ar.model=reviews.model)",
                        (pet_id, self.settings.recognizer_key),
                    )
                    conn.execute(
                        "DELETE FROM automatic_reviews WHERE EXISTS (SELECT 1 FROM reviews r "
                        "WHERE r.detection_id=automatic_reviews.detection_id "
                        "AND r.model=automatic_reviews.model AND r.pet_id=? "
                        "AND r.status='pending')",
                        (pet_id,),
                    )
                self._review_changed(conn)
            # Persisted revision makes an interrupted backlog recheck resume on restart.
            self.gallery_changed()
        return next(pet for pet in self.pets() if pet["id"] == pet_id)

    def rematch(self):
        with self.model_lock:
            revision = self.store.state("gallery_revision", "0")
            with self.recheck_lock:
                self.recheck = {
                    "revision": revision,
                    "model": self.settings.recognizer_key,
                    "state": "preparing",
                    "completed": 0,
                    "total": None,
                    "automatic_matches": 0,
                    "started_at": datetime.now(UTC).isoformat(),
                    "finished_at": None,
                    "error": "",
                }
        try:
            with self.model_lock:
                detections = self.store.rows(
                    "SELECT d.* FROM detections d JOIN assets a ON a.id=d.asset_id "
                    "JOIN reviews r ON r.detection_id=d.id AND r.model=? "
                    "WHERE d.revision=a.revision AND d.detector=? AND r.status='pending'",
                    (self.settings.recognizer_key, self.settings.detector_key),
                )
                self._recheck_update(revision, total=len(detections))
                gallery = self.references()
            # Policy fitting is CPU work over a gallery snapshot. Keep it out
            # of the human-decision lock, then validate the revision below.
            self.policies(gallery, refresh=True, revision=revision)
            self._recheck_update(revision, state="running")
            automatic_matches = 0
            for index, detection in enumerate(detections):
                with self.model_lock:
                    if self.stop_event.is_set():
                        self._recheck_update(revision, state="interrupted")
                        return
                    if revision != self.store.state("gallery_revision", "0"):
                        self.rematch_requested.set()
                        return
                    automatic_matches += bool(self.evaluate(detection, gallery))
                    self._recheck_update(
                        revision, completed=index + 1, automatic_matches=automatic_matches
                    )
                # Offer waiting foreground threads the lock between background
                # items instead of immediately reacquiring it for the next crop.
                time.sleep(0)
            with self.model_lock:
                if revision != self.store.state("gallery_revision", "0"):
                    self.rematch_requested.set()
                    return
                with self.recheck_lock:
                    result = {
                        **self.recheck,
                        "state": "complete",
                        "finished_at": datetime.now(UTC).isoformat(),
                    }
                with self.store.connection() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO state VALUES ('matched_gallery_revision',?)",
                        (revision,),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO state VALUES ('last_recheck',?)",
                        (json.dumps(result),),
                    )
                with self.recheck_lock:
                    self.recheck = result
        except Exception as error:
            self._recheck_update(revision, state="failed", error=self.safe_error(error))
            raise

    def _recheck_update(self, revision, **values):
        with self.recheck_lock:
            if self.recheck.get("revision") == revision:
                self.recheck.update(values)

    def recheck_status(self):
        revision = self.store.state("gallery_revision", "0")
        with self.recheck_lock:
            result = dict(self.recheck)
        current = result.get("revision") == revision and result.get("model") == (
            self.settings.recognizer_key
        )
        pending = self.suggestions_pending()
        if not current or result.get("state") == "interrupted":
            result = {
                "state": "queued" if pending else "idle",
                "completed": 0,
                "total": None,
                "automatic_matches": 0,
                "finished_at": None,
                "error": "",
            }
        total = result.get("total")
        result["percent"] = (
            100
            if result["state"] == "complete"
            else min(99, int(result["completed"] * 100 / total))
            if total
            else None
        )
        return result

    def suggestions_pending(self):
        return self.store.state("gallery_revision", "0") != self.store.state(
            "matched_gallery_revision", "0"
        )

    def gallery_changed(self):
        with self.model_lock:
            if self.batch_depth:
                self.batch_gallery_dirty = True
                return
            with self.store.connection() as conn:
                conn.execute(
                    "INSERT INTO state VALUES ('gallery_revision','1') ON CONFLICT(key) "
                    "DO UPDATE SET value=CAST(value AS INTEGER)+1"
                )
                revision = conn.execute(
                    "SELECT value FROM state WHERE key='gallery_revision'"
                ).fetchone()[0]
            with self.recheck_lock:
                self.recheck = {
                    "revision": revision,
                    "model": self.settings.recognizer_key,
                    "state": "queued",
                    "completed": 0,
                    "total": None,
                    "automatic_matches": 0,
                    "finished_at": None,
                    "error": "",
                }
            self.rematch_requested.set()
            self.rematch_not_before = time.monotonic() + 0.75
            self.wake_event.set()
            self.group_cache = None
        if self.settings.demo:
            self.rematch_requested.clear()
            self.rematch()

    def recommendations(self, rows):
        """Fresh visible suggestions from cached vectors, without GPU work or tag writes."""
        gallery = self.references(cached_only=True)
        policies = self.policies(gallery)
        names = {pet["id"]: pet["name"] for pet in gallery}
        excluded = {}
        for reference in self.store.rows(
            "SELECT pet_id,detection_id FROM reference_images WHERE kind='negative'"
        ):
            excluded.setdefault(reference["detection_id"], set()).add(reference["pet_id"])
        for row in rows:
            row["recommendation"] = None
            if row["status"] != "pending":
                continue
            vector = self.store.embedding(row["id"], self.settings.recognizer_key)
            if vector is None:
                continue
            match = match_pet(
                vector,
                row["species"],
                [pet for pet in gallery if pet["id"] not in excluded.get(row["id"], set())],
                self.settings.match_threshold,
                self.settings.match_margin,
                self.settings.min_references,
                policies,
            )
            if match.pet_id is None:
                continue
            policy = policies.get(match.pet_id, {})
            threshold = policy.get("threshold", self.settings.match_threshold)
            margin = policy.get("margin", self.settings.match_margin)
            minimum = policy.get("min_references", self.settings.min_references)
            row["recommendation"] = {
                "pet_id": match.pet_id,
                "pet_name": names[match.pet_id],
                "score": match.score,
                "margin": match.margin,
                "references": match.references,
                "confident": match.confident,
                "gates": [
                    {
                        "label": "Examples",
                        "value": match.references,
                        "required": minimum,
                        "passed": match.references >= minimum,
                    },
                    {
                        "label": "Similarity",
                        "value": match.score,
                        "required": threshold,
                        "passed": match.score >= threshold,
                    },
                    {
                        "label": "Lead over look-alikes",
                        "value": match.margin,
                        "required": margin,
                        "passed": match.margin >= margin,
                    },
                ],
            }
        return rows

    def add_pet(self, name, species):
        pet = {"id": str(uuid.uuid4()), "name": name, "species": species, "tag": f"Pets/{name}"}
        self.store.execute("INSERT INTO pets VALUES (:id,:name,:species,:tag)", pet)
        self.policy_cache = None
        self.group_cache = None
        return pet

    def add_reference(self, pet_id, detection_id, kind):
        with self.model_lock:
            pet = self.store.one("SELECT * FROM pets WHERE id=?", (pet_id,))
            detection = self.store.one("SELECT * FROM detections WHERE id=?", (detection_id,))
            if pet is None or detection is None:
                raise ValueError("Pet or detection not found")
            if pet["species"] != detection["species"]:
                raise ValueError("Reference species must match this pet")
            self.store.execute(
                "INSERT INTO reference_images VALUES (?,?,?) ON CONFLICT(pet_id,detection_id) "
                "DO UPDATE SET kind=excluded.kind",
                (pet_id, detection_id, kind),
            )
            self.gallery_changed()

    def decide(
        self, detection_id, pet_id, status, learn=True, expected_pet_id=None, expected_status=None
    ):
        with self.model_lock:
            self.group_cache = None
            detection = self.store.one(
                "SELECT d.* FROM detections d JOIN assets a ON a.id=d.asset_id "
                "WHERE d.id=? AND d.revision=a.revision AND d.detector=?",
                (detection_id, self.settings.detector_key),
            )
            if detection is None:
                raise ValueError("Detection no longer matches the current asset revision")
            if pet_id:
                pet = self.store.one("SELECT * FROM pets WHERE id=?", (pet_id,))
                if pet is None:
                    raise ValueError("Pet not found")
            if status == "confirmed" and not pet_id:
                raise ValueError("Choose the pet to confirm")
            row = self.store.one(
                "SELECT * FROM reviews WHERE detection_id=? AND model=?",
                (detection_id, self.settings.recognizer_key),
            )
            if row is None:
                raise ValueError("This detection is not ready for review")
            if expected_pet_id is not None and row["pet_id"] != expected_pet_id:
                raise ValueError("Assignment changed; refresh before correcting this photo")
            if expected_status is not None and row["status"] != expected_status:
                raise ValueError("Assignment changed; refresh before correcting this photo")
            changed = row["pet_id"] != pet_id or row["status"] != status
            gallery_updated = False
            with self.store.connection() as conn:
                if status == "confirmed" and pet["species"] != detection["species"]:
                    # A human identity assignment also corrects the detector's species.
                    conn.execute(
                        "UPDATE detections SET species=? WHERE id=?",
                        (pet["species"], detection_id),
                    )
                    gallery_updated = bool(
                        conn.execute(
                            "DELETE FROM reference_images WHERE detection_id=? AND pet_id IN "
                            "(SELECT id FROM pets WHERE species<>?)",
                            (detection_id, pet["species"]),
                        ).rowcount
                    )
                conn.execute(
                    "DELETE FROM automatic_reviews WHERE detection_id=? AND model=?",
                    (detection_id, self.settings.recognizer_key),
                )
                conn.execute(
                    "DELETE FROM automatic_sources WHERE detection_id=? AND model=?",
                    (detection_id, self.settings.recognizer_key),
                )
                conn.execute(
                    "UPDATE reviews SET pet_id=?,status=?,published=?,error=NULL "
                    "WHERE detection_id=? AND model=?",
                    (
                        pet_id,
                        status,
                        0 if changed else row["published"],
                        detection_id,
                        self.settings.recognizer_key,
                    ),
                )
                self._review_changed(conn)
                if changed and row["status"] == "confirmed" and row["pet_id"]:
                    old_pet = conn.execute(
                        "SELECT tag FROM pets WHERE id=?", (row["pet_id"],)
                    ).fetchone()
                    if not self.settings.demo:
                        conn.execute(
                            "INSERT OR IGNORE INTO tag_removals VALUES (?,?,?,?)",
                            (
                                detection_id,
                                self.settings.recognizer_key,
                                detection["asset_id"],
                                old_pet["tag"],
                            ),
                        )
                    gallery_updated |= bool(
                        conn.execute(
                            "DELETE FROM reference_images WHERE pet_id=? AND detection_id=? "
                            "AND kind='positive'",
                            (row["pet_id"], detection_id),
                        ).rowcount
                    )
                if status == "pending" and row["pet_id"]:
                    # Removing a name returns the photo to Unidentified and
                    # teaches that the old identity was wrong.
                    conn.execute(
                        "INSERT INTO reference_images VALUES (?,?,'negative') "
                        "ON CONFLICT(pet_id,detection_id) DO UPDATE SET kind='negative'",
                        (row["pet_id"], detection_id),
                    )
                    gallery_updated = True
                if learn and status == "confirmed":
                    # Only an explicit human assignment can teach the gallery.
                    conn.execute(
                        "DELETE FROM reference_images WHERE detection_id=? AND kind='positive' "
                        "AND pet_id<>?",
                        (detection_id, pet_id),
                    )
                    conn.execute(
                        "INSERT INTO reference_images VALUES (?,?,'positive') "
                        "ON CONFLICT(pet_id,detection_id) DO UPDATE SET kind='positive'",
                        (pet_id, detection_id),
                    )
                    if row["pet_id"] and row["pet_id"] != pet_id:
                        conn.execute(
                            "INSERT INTO reference_images SELECT id,?,'negative' FROM pets "
                            "WHERE id=? AND species=? "
                            "ON CONFLICT(pet_id,detection_id) DO UPDATE SET kind='negative'",
                            (detection_id, row["pet_id"], pet["species"]),
                        )
            if gallery_updated or (learn and status == "confirmed"):
                self.gallery_changed()
        self.wake_event.set()

    @contextmanager
    def batch_updates(self):
        with self.model_lock:
            self.batch_depth += 1
            try:
                yield
            finally:
                self.batch_depth -= 1
                if not self.batch_depth and self.batch_gallery_dirty:
                    self.batch_gallery_dirty = False
                    self.gallery_changed()

    def bulk_decide(self, items):
        results = []
        # Publication cannot race the batch; individual invalid rows remain untouched.
        with self.batch_updates():
            for item in items:
                try:
                    self.decide(
                        item["detection_id"],
                        item["pet_id"],
                        item["status"],
                        item["learn"],
                        item.get("expected_pet_id"),
                        item.get("expected_status"),
                    )
                    results.append({"id": item["detection_id"], "saved": True})
                except ValueError as error:
                    results.append(
                        {"id": item["detection_id"], "saved": False, "error": str(error)}
                    )
        return results

    def collections(self):
        # Albums describe current names. Immich publication is background sync,
        # and must not make a newly named photo disappear until the network finishes.
        published = "r.status='confirmed'"
        counts = {
            row["pet_id"]: row
            for row in self.store.rows(
                "SELECT r.pet_id,count(DISTINCT d.asset_id) photo_count,"
                "count(DISTINCT CASE WHEN ar.detection_id IS NOT NULL THEN d.asset_id END) "
                "auto_photo_count,(SELECT refs.detection_id FROM reference_images refs "
                "WHERE refs.pet_id=r.pet_id AND refs.kind='positive' ORDER BY refs.rowid LIMIT 1) "
                "cover_id FROM reviews r "
                "JOIN detections d ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
                "LEFT JOIN automatic_reviews ar ON ar.detection_id=r.detection_id "
                "AND ar.model=r.model "
                f"WHERE {published} AND r.model=? AND d.detector=? AND d.revision=a.revision "
                "GROUP BY r.pet_id",
                (self.settings.recognizer_key, self.settings.detector_key),
            )
        }
        return [
            {
                **pet,
                **counts.get(
                    pet["id"], {"photo_count": 0, "auto_photo_count": 0, "cover_id": None}
                ),
            }
            for pet in self.pets()
        ]

    def groups(self):
        """Actionable snapshots stay on screen while background suggestions change.

        Membership is stable for a session; a fresh score never invalidates a
        user's selection. Submission still checks each current review so an
        automatic match or another person's decision cannot be overwritten.
        """
        model = self.settings.recognizer_key

        def signature():
            return (self.review_generation, model, self.settings.detector_key)

        # Capture the generation after any in-flight human transaction commits.
        # The large read then runs without the decision lock. Concurrent changes
        # invalidate this cache generation, and submission revalidates every row.
        with self.model_lock:
            revision = signature()
        with self.group_lock:
            if self.group_cache and self.group_cache[0] == revision:
                return self.group_cache[1]
        rows = self.store.rows(
            "WITH tagged AS MATERIALIZED (SELECT DISTINCT d.asset_id FROM reviews r "
            "JOIN detections d ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
            "WHERE r.model=? AND d.detector=? AND d.revision=a.revision "
            + "AND r.status='confirmed' "
            + "UNION SELECT asset_id FROM tag_removals) "
            "SELECT d.id,d.asset_id,d.species,a.name,r.pet_id,r.score,r.margin,e.vector "
            "FROM reviews r JOIN detections d ON d.id=r.detection_id "
            "JOIN assets a ON a.id=d.asset_id JOIN embeddings e ON e.detection_id=d.id "
            "AND e.model=r.model WHERE r.model=? AND d.detector=? AND d.revision=a.revision "
            "AND r.status='pending' AND NOT EXISTS(SELECT 1 FROM tagged t "
            "WHERE t.asset_id=d.asset_id) ORDER BY r.score DESC,d.id",
            (model, self.settings.detector_key, model, self.settings.detector_key),
        )
        with self.group_lock:
            current = {row["id"]: row for row in rows}
            if self.group_snapshots.get("pipeline") != (model, self.settings.detector_key):
                self.group_snapshots = {"pipeline": (model, self.settings.detector_key)}
            snapshots = self.group_snapshots.setdefault("items", {})
            now = time.monotonic()
            # Bounded, temporary selection snapshots, not a history of labels.
            snapshots = {key: value for key, value in snapshots.items() if now - value[0] < 3600}
            self.group_snapshots["items"] = snapshots
            tracked = {
                member["id"] for _, group in snapshots.values() for member in group["members"]
            }
            unseen = [row for row in rows if row["id"] not in tracked]
            for row in unseen:
                row["vector"] = np.frombuffer(row["vector"], dtype=np.float32)
            for group in similar_groups(unseen):
                snapshots[group["id"]] = (now, group)
            names = {pet["id"]: pet["name"] for pet in self.store.rows("SELECT id,name FROM pets")}
            groups = []
            for _, snapshot in snapshots.values():
                members = [
                    current[member["id"]]
                    for member in snapshot["members"]
                    if member["id"] in current
                ]
                if not members:
                    continue
                members = [{k: v for k, v in row.items() if k != "vector"} for row in members]
                # A suggestion may change, but the selected photo set and ID do not.
                choices = {}
                for member in members:
                    choices[member["pet_id"]] = choices.get(member["pet_id"], 0) + 1
                pet_id = max(choices, key=choices.get)
                indexes = sorted(
                    set([0, len(members) // 3, 2 * len(members) // 3, len(members) - 1])
                )
                groups.append(
                    {
                        **snapshot,
                        "pet_id": pet_id,
                        "pet_name": names.get(pet_id),
                        "members": members,
                        "crop_count": len(members),
                        "photo_count": len({row["asset_id"] for row in members}),
                        "asset_ids": sorted({row["asset_id"] for row in members}),
                        "samples": [members[index] for index in indexes],
                    }
                )
            groups.sort(key=lambda group: (-group["photo_count"], group["id"]))
            if revision == signature():
                self.group_cache = (revision, groups)
            return groups

    def _references_for(self, detection_id):
        return self.store.rows(
            "SELECT pet_id,kind FROM reference_images WHERE detection_id=? ORDER BY pet_id,kind",
            (detection_id,),
        )

    def decide_group(self, group_id, pet_id, excluded, skip=False):
        return self.decide_groups([group_id], pet_id, skip, {group_id: excluded})

    def decide_groups(self, group_ids, pet_id, skip=False, excluded=None):
        # The browser selected a snapshot already served by groups(). Rebuilding
        # all remaining suggestions here made each save slow during recognition.
        excluded = excluded or {}
        ids = set()
        with self.group_lock:
            snapshots = self.group_snapshots.get("items", {})
            for group_id in group_ids:
                entry = snapshots.get(group_id)
                if entry is None or time.monotonic() - entry[0] >= 3600:
                    raise ValueError("This selection expired. Refresh to see the current photos.")
                members = {row["id"] for row in entry[1]["members"]}
                omitted = set(excluded.get(group_id, []))
                if not omitted <= members:
                    raise ValueError("An excluded photo is not in this group")
                ids.update(members - omitted)
        if not ids:
            raise ValueError("Select at least one photo")
        if len(ids) > 5000:
            raise ValueError("Select up to 5,000 photos at a time")
        with self.model_lock:
            pet = None if skip else self.store.one("SELECT * FROM pets WHERE id=?", (pet_id,))
            if not skip and not pet:
                raise ValueError("Choose a pet")
            selected = json.dumps(sorted(ids))
            # One transaction for the whole human action, even across many clusters.
            # Changed or automatically handled rows are skipped, never overwritten.
            with self.store.connection() as conn:
                before = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT r.*,d.species,d.asset_id FROM reviews r JOIN detections d "
                        "ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
                        "WHERE d.id IN (SELECT value FROM json_each(?)) AND r.model=? "
                        "AND d.detector=? AND d.revision=a.revision AND r.status='pending'",
                        (selected, self.settings.recognizer_key, self.settings.detector_key),
                    )
                ]
                if not before:
                    raise ValueError(
                        "These photos have already been handled. "
                        "Your other selections are still available."
                    )
                references = {}
                for ref in conn.execute(
                    "SELECT * FROM reference_images WHERE detection_id IN "
                    "(SELECT value FROM json_each(?)) ORDER BY pet_id,kind",
                    (selected,),
                ):
                    references.setdefault(ref["detection_id"], []).append(
                        {"pet_id": ref["pet_id"], "kind": ref["kind"]}
                    )
                for row in before:
                    detection_id = row["detection_id"]
                    row["references"] = references.get(detection_id, [])
                    if not skip and pet["species"] != row["species"]:
                        conn.execute(
                            "UPDATE detections SET species=? WHERE id=?",
                            (pet["species"], detection_id),
                        )
                        conn.execute(
                            "DELETE FROM reference_images WHERE detection_id=? AND pet_id IN "
                            "(SELECT id FROM pets WHERE species<>?)",
                            (detection_id, pet["species"]),
                        )
                    conn.execute(
                        "DELETE FROM automatic_reviews WHERE detection_id=? AND model=?",
                        (detection_id, self.settings.recognizer_key),
                    )
                    conn.execute(
                        "DELETE FROM automatic_sources WHERE detection_id=? AND model=?",
                        (detection_id, self.settings.recognizer_key),
                    )
                    conn.execute(
                        "UPDATE reviews SET pet_id=?,status=?,published=0,error=NULL "
                        "WHERE detection_id=? AND model=?",
                        (
                            None if skip else pet_id,
                            "rejected" if skip …6924 tokens truncated…      loadingImages--;
        drainCrops();
      });
  }
}
function photoImage(row, size = 320) {
  return image(row.id, row.name, "", size, false, "photo");
}
function assignmentsFor(item) {
  return item.assignment_ids || [item.id];
}
function photoFailed(item, failedIds) {
  return assignmentsFor(item).some((id) => failedIds.has(id));
}
function operationId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
function keyOf(item) {
  return item.type === "group" ? "g:" + item.id : "d:" + item.id;
}
function photoIds(items) {
  return new Set(
    items.flatMap((item) =>
      item.type === "group"
        ? item.asset_ids || item.samples.map((row) => row.asset_id)
        : [item.asset_id],
    ),
  );
}
function selectionItems() {
  return (view?.items || []).filter((item) => selected.has(keyOf(item)));
}
function navigate(hash) {
  location.hash = hash;
}
function route() {
  const parts = (location.hash.slice(1) || "library").split("/");
  if (parts[0] === "pet" && petById(parts[1]))
    return {
      type: "album",
      petId: parts[1],
      candidates: parts[2] === "more",
      key: parts.join("/"),
    };
  if (parts[0] === "unidentified")
    return {
      type: "unidentified",
      petId: parts[1] || "",
      key: parts.join("/"),
    };
  if (parts[0] === "photos")
    return { type: "album", petId: "", candidates: false, key: "photos" };
  if (["settings", "activity"].includes(parts[0]))
    return { type: parts[0], key: parts[0] };
  return { type: "library", key: "library" };
}
function heading(title, subtitle) {
  const container = el("div", "page-heading"),
    text = el("div");
  text.append(el("h1", "", title));
  if (subtitle) text.append(el("p", "", subtitle));
  container.append(text);
  $("view").append(container);
  return container;
}
function empty(container, title, detail) {
  const message = el("div", "empty");
  message.append(
    el("div", "empty-mark", "◌"),
    el("h2", "", title),
    el("p", "", detail),
  );
  container.append(message);
}
function renderNavigation() {
  const nav = $("pet-navigation");
  nav.replaceChildren();
  pets.forEach((pet) => {
    const a = link("", "#pet/" + pet.id, "side-link");
    a.append(
      image(pet.cover_id, "", "nav-avatar", 160),
      el("span", "", pet.name),
      el("small", "nav-number", number(pet.photo_count)),
    );
    nav.append(a);
  });
  $("unidentified-count").textContent = number(
    status.review_summary?.untagged_photos,
  );
  document.querySelectorAll(".side-link,.mobile-navigation a").forEach((a) => {
    const current = location.hash || "#library";
    a.classList.toggle(
      "active",
      a.hash === current ||
        (a.hash.startsWith("#pet/") && current.startsWith(a.hash)) ||
        (a.hash === "#unidentified" && current.startsWith(a.hash)),
    );
  });
}
function renderActivity() {
  const check = status.recheck || {},
    queued = outbox.pending;
  const active = ["queued", "preparing", "running"].includes(check.state);
  const syncing = status.review_summary?.pending_tag_changes || 0;
  const failed =
    outbox.failed.length || status.last_error || check.state === "failed";
  $("activity-label").textContent = queued
    ? `Saving ${plural(queued, "change")}`
    : failed
      ? "Needs attention"
      : active
        ? `Organizing${check.percent != null ? " · " + check.percent + "%" : "…"}`
        : syncing
          ? `Syncing ${plural(syncing, "photo")}`
          : "Up to date";
  $("activity-indicator").classList.toggle(
    "working",
    Boolean(queued || active || syncing),
  );
  $("activity-indicator").classList.toggle("failed", Boolean(failed));
  $("demo-banner").classList.toggle("hidden", !status.demo);
  $("error-banner").classList.toggle("hidden", !status.last_error);
  $("error-banner").textContent = status.last_error || "";
  renderUndo();
}
async function refreshStatus() {
  if (polling) return;
  polling = true;
  try {
    const [nextStatus, nextPets, groups] = await Promise.all([
      api("status"),
      api("collections"),
      api("groups?limit=1"),
    ]);
    unidentifiedCount = groups.total_photos;
    status = nextStatus;
    pets = nextPets;
    if (
      !lastUndo &&
      status.undo_group &&
      status.undo_group.id !== dismissedUndoId
    )
      lastUndo = {
        kind: "group",
        id: status.undo_group.id,
        label: status.undo_group.skip
          ? "Photos hidden"
          : "Added to " + (petById(status.undo_group.pet_id)?.name || "pet"),
      };
    renderNavigation();
    updateAlbumCount();
    renderActivity();
    if (view?.type === "activity") renderActivityPage();
  } finally {
    polling = false;
  }
}
let albumsRefreshing = false;
function updateAlbumCount() {
  const count = document.querySelector(".album-title p");
  if (count && view?.petId && petById(view.petId))
    count.textContent = plural(petById(view.petId).photo_count, "photo");
}
async function refreshAlbums() {
  if (albumsRefreshing) return;
  albumsRefreshing = true;
  try {
    pets = await api("collections");
    renderNavigation();
    updateAlbumCount();
  } finally {
    albumsRefreshing = false;
  }
}
async function openRoute() {
  const version = ++routeVersion;
  selected.clear();
  selectMode = false;
  lastSelected = -1;
  view = {
    ...route(),
    items: [],
    total: 0,
    offset: 0,
    loading: false,
    source: "all",
  };
  $("view").replaceChildren();
  renderSelection();
  renderNavigation();
  const current = view;
  if (current.type === "library") await renderLibrary(version);
  else if (current.type === "album") {
    renderAlbumShell();
    await loadItems(false, version);
  } else if (current.type === "unidentified") {
    renderUnidentifiedShell();
    await loadItems(false, version);
  } else if (current.type === "settings") renderSettings();
  else if (current.type === "activity") renderActivityPage();
  if (version === routeVersion)
    window.scrollTo({ top: 0, behavior: "instant" });
}
async function renderLibrary(version) {
  const top = heading("Library");
  top.append(link("All pet photos", "#photos", "text-button"));
  const covers = el("div", "pet-covers");
  $("view").append(covers);
  pets.forEach((pet) => {
    const cover = button(
      "",
      "pet-cover",
      () => navigate("pet/" + pet.id),
      `Open ${pet.name}`,
    );
    if (pet.cover_id) cover.append(image(pet.cover_id, pet.name, "", 640));
    else cover.append(el("div", "cover-initial", pet.name[0]));
    const caption = el("div", "cover-caption");
    caption.append(
      el("h2", "", pet.name),
      el("p", "", plural(pet.photo_count, "photo")),
    );
    cover.append(caption);
    covers.append(cover);
  });
  if (!pets.length) {
    empty(
      covers,
      "Meet your first pet",
      "Add a name to start collecting their photos.",
    );
    covers.append(link("Add a pet", "#settings", "primary"));
  }
  const unknown = el("div", "section-title");
  unknown.append(
    el("h2", "", "Unidentified"),
    link("Browse photos →", "#unidentified", "text-button"),
  );
  $("view").append(unknown);
  const line = el(
    "p",
    "photo-caption-note",
    "A place for photos without a name. Organize them whenever you like.",
  );
  $("view").append(line);
  const unknownStrip = el("div", "photo-strip");
  $("view").append(unknownStrip);
  const unknownPromise = api("groups?limit=8").then((data) => {
    if (version !== routeVersion) return;
    unidentifiedCount = data.total_photos;
    $("unidentified-count").textContent = number(unidentifiedCount);
    data.items.slice(0, 8).forEach((group) => {
      const tile = button(
        "",
        "photo-tile",
        () => navigate("unidentified"),
        `Browse ${plural(group.photo_count, "unidentified photo")}`,
      );
      tile.append(image(group.samples[0]?.id, group.samples[0]?.name));
      if (group.photo_count > 1)
        tile.append(el("span", "tile-count", group.photo_count + " photos"));
      unknownStrip.append(tile);
    });
    if (!data.items.length)
      line.textContent = "All your detected pet photos have a name.";
  });
  const strips = pets.map(async (pet) => {
    const section = el("div", "section-title");
    section.append(
      el("h2", "", pet.name),
      link("View album →", "#pet/" + pet.id, "text-button"),
    );
    const strip = el("div", "photo-strip");
    $("view").append(section, strip);
    const data = await api(
      `detections?status=confirmed&tagging=all&pet_id=${pet.id}&sort=newest&limit=10&unique_photos=true`,
    );
    if (version !== routeVersion) return;
    libraryPreviews.set(pet.id, data.items);
    data.items.forEach((row, index) => {
      const tile = button(
        "",
        "photo-tile",
        () => openViewer(data.items, index),
        `Open ${row.name}`,
      );
      tile.append(photoImage(row));
      strip.append(tile);
    });
  });
  await Promise.allSettled([unknownPromise, ...strips]);
}
function renderAlbumShell() {
  const pet = petById(view.petId),
    top = el("div", "page-heading");
  const identity = el("div", "album-heading");
  if (pet) identity.append(image(pet.cover_id, "", "album-avatar", 160));
  const title = el("div", "album-title");
  title.append(
    el("h1", "", pet?.name || "All pet photos"),
    el(
      "p",
      "",
      pet ? plural(pet.photo_count, "photo") : "Your named pets, together",
    ),
  );
  if (pet) {
    const nameHeading = title.querySelector("h1");
    const switcher = el("div", "album-switcher");
    nameHeading.replaceWith(switcher);
    const chevron = el("span", "album-chevron", "⌄");
    chevron.setAttribute("aria-hidden", "true");
    switcher.append(nameHeading, chevron);
    const picker = el("select", "album-pet-picker");
    picker.setAttribute("aria-label", "Switch pet");
    pets.forEach((other) => {
      const option = el("option", "", other.name);
      option.value = other.id;
      picker.append(option);
    });
    picker.value = pet.id;
    picker.addEventListener("change", () =>
      navigate("pet/" + picker.value + (view.candidates ? "/more" : "")),
    );
    switcher.append(picker);
  }
  identity.append(title);
  top.append(identity);
  if (!view.candidates && view.petId) {
    const select = button("Select", "secondary", () => {
      selectMode = !selectMode;
      selected.clear();
      renderGrid();
      renderSelection();
      select.textContent = selectMode ? "Done" : "Select";
    });
    select.id = "album-select";
    top.append(select);
  }
  $("view").append(top);

  if (pet) {
    const tabs = el("nav", "album-tabs");
    tabs.setAttribute("aria-label", "Album views");
    const saved = link(
      "Photos",
      "#pet/" + pet.id,
      !view.candidates ? "active" : "",
    );
    const more = link(
      "More photos",
      "#pet/" + pet.id + "/more",
      view.candidates ? "active" : "",
    );
    tabs.append(saved, more);
    $("view").append(tabs);
  }
  const toolbar = el("div", "view-toolbar"),
    actions = el("div", "toolbar-actions");
  toolbar.append(
    el(
      "p",
      "",
      view.candidates
        ? `Select photos of ${pet.name}. Add them together.`
        : view.petId
          ? "Select photos to move them or remove a name."
          : "Open a photo to see its pets.",
    ),
  );
  if (view.petId)
    actions.append(button("Select shown", "text-button", selectAllVisible));
  actions.append(
    button("↻", "text-button", () => reloadPhotos(), "Refresh photos"),
  );
  if (!view.candidates && view.petId) {
    const source = el("select");
    source.setAttribute("aria-label", "Photo source");
    [
      ["all", "All photos"],
      ["automatic", "Automatic"],
      ["manual", "Named by you"],
    ].forEach(([value, text]) => {
      const option = el("option", "", text);
      option.value = value;
      source.append(option);
    });
    source.addEventListener("change", () => {
      view.source = source.value;
      reloadPhotos();
    });
    actions.prepend(source);
  }
  toolbar.append(actions);
  $("view").append(toolbar);
  addGrid();
}
function renderUnidentifiedShell() {
  heading("Unidentified", "Select photos. Give them a name together.");
  const toolbar = el("div", "view-toolbar"),
    chips = el("nav", "filter-chips");
  chips.setAttribute("aria-label", "Suggested pet");
  [["", "All photos"], ...pets.map((pet) => [pet.id, pet.name])].forEach(
    ([id, name]) =>
      chips.append(
        link(
          name,
          "#unidentified" + (id ? "/" + id : ""),
          "filter-chip" + (id === view.petId ? " active" : ""),
        ),
      ),
  );
  const actions = el("div", "toolbar-actions");
  actions.append(
    button("Select shown", "text-button", selectAllVisible),
    button("↻", "text-button", reloadPhotos, "Refresh photos"),
  );
  toolbar.append(chips, actions);
  $("view").append(toolbar);
  addGrid();
}
function addGrid() {
  const grid = el("div", "photo-grid");
  grid.id = "photo-grid";
  grid.setAttribute("aria-label", "Photos");
  for (let i = 0; i < 18; i++) grid.append(el("div", "skeleton"));
  const more = button("Show more photos", "secondary load-more", () =>
    loadItems(true),
  );
  more.id = "load-more";
  more.classList.add("hidden");
  $("view").append(grid, more);
}
async function loadItems(append = false, version = routeVersion) {
  if (view.loading) return;
  view.loading = true;
  const generation = boardGeneration;
  let retryRead = false;
  const current = view,
    groups = current.type === "unidentified" || current.candidates;
  const query = new URLSearchParams(
    groups
      ? { limit: 48, offset: append ? current.offset : 0 }
      : {
          status: "confirmed",
          tagging: "all",
          limit: 72,
          offset: append ? current.offset : 0,
          sort: "newest",
          source: current.source,
          unique_photos: true,
        },
  );
  if (current.petId) {
    query.set("pet_id", current.petId);
    if (groups) query.set("near_match", "true");
  }
  try {
    const data = await api((groups ? "groups?" : "detections?") + query);
    if (version !== routeVersion || current !== view) return;
    if (generation !== boardGeneration) {
      retryRead = true;
      return;
    }
    if (groups) {
      unidentifiedCount = data.total_photos;
      $("unidentified-count").textContent = number(unidentifiedCount);
    }
    const items = data.items
      .map((item) => ({ ...item, type: groups ? "group" : "photo" }))
      .filter((item) =>
        groups ? !removedGroups.has(item.id) : !removedPhotos.has(item.id),
      );
    const existing = new Set(append ? view.items.map(keyOf) : []);
    lastSelected = -1;
    view.items = append
      ? [...view.items, ...items.filter((item) => !existing.has(keyOf(item)))]
      : items;
    view.offset = (append ? view.offset : 0) + data.items.length;
    view.total = groups ? data.total_groups : data.total;
    renderGrid();
    renderSelection();
    $("load-more").classList.toggle("hidden", view.offset >= view.total);
  } catch (error) {
    if (version === routeVersion) {
      if (!view.items.length) {
        $("photo-grid").replaceChildren();
        empty($("photo-grid"), "Photos could not load", error.message);
      } else showError(error);
    }
  } finally {
    current.loading = false;
    if (retryRead && current === view && version === routeVersion)
      loadItems(append, version).catch(showError);
  }
}
function reloadPhotos() {
  if (!view || view.loading) return;
  selected.clear();
  view.offset = 0;
  return loadItems(false);
}
function renderGrid() {
  const grid = $("photo-grid");
  if (!grid) return;
  grid.classList.toggle(
    "selecting",
    Boolean(
      selectMode ||
        view.candidates ||
        view.type === "unidentified" ||
        selected.size,
    ),
  );
  grid.replaceChildren();
  view.items.forEach((item, index) => grid.append(photoTile(item, index)));
  if (!view.items.length)
    empty(
      grid,
      view.candidates
        ? "No more suggestions here"
        : view.type === "unidentified"
          ? "Nothing unidentified here"
          : "No photos yet",
      view.candidates
        ? "New matches will appear as your library grows."
        : view.type === "unidentified"
          ? "Your photos are already in pet albums."
          : "Photos will appear here as they are named.",
    );
}
function photoTile(item, index) {
  const key = keyOf(item),
    group = item.type === "group";
  const tile = el("div", "photo-tile" + (selected.has(key) ? " selected" : ""));
  tile.dataset.key = key;
  tile.tabIndex = 0;
  tile.setAttribute("role", group || selectMode ? "checkbox" : "button");
  tile.setAttribute(
    "aria-label",
    group
      ? `Select ${plural(item.photo_count, "photo")}${item.pet_name ? " suggested as " + item.pet_name : ""}`
      : `Open ${item.name}`,
  );
  if (group || selectMode)
    tile.setAttribute("aria-checked", String(selected.has(key)));
  const act = (event) => {
    if (group || selectMode || selected.size)
      toggleItem(item, index, event.shiftKey);
    else openViewer(view.items, index);
  };
  tile.addEventListener("click", act);
  tile.addEventListener("keydown", (event) => {
    if (event.target === tile && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      act(event);
    }
  });
  if (group) {
    const mosaic = el("div", "tile-mosaic");
    item.samples.forEach((row) => mosaic.append(image(row.id, row.name)));
    tile.append(mosaic);
    if (item.photo_count > 1)
      tile.append(el("span", "tile-count", "▣ " + number(item.photo_count)));
  } else tile.append(photoImage(item));
  tile.append(el("span", "photo-check", selected.has(key) ? "✓" : ""));
  if (group)
    tile.append(
      button(
        "⤢",
        "tile-zoom",
        () => openDetail(item),
        `Inspect ${plural(item.photo_count, "similar photo")}`,
      ),
    );
  return tile;
}
function toggleItem(item, index, range = false) {
  const value = !selected.has(keyOf(item));
  if (range && lastSelected >= 0) {
    for (
      let i = Math.min(index, lastSelected);
      i <= Math.min(view.items.length - 1, Math.max(index, lastSelected));
      i++
    ) {
      const key = keyOf(view.items[i]);
      if (value) selected.add(key);
      else selected.delete(key);
    }
  } else if (value) selected.add(keyOf(item));
  else selected.delete(keyOf(item));
  lastSelected = index;
  syncSelectionTiles();
  renderSelection();
}
function syncSelectionTiles() {
  const grid = $("photo-grid");
  if (!grid) return;
  grid.classList.toggle(
    "selecting",
    Boolean(
      selectMode ||
        view.candidates ||
        view.type === "unidentified" ||
        selected.size,
    ),
  );
  grid.querySelectorAll(".photo-tile").forEach((tile) => {
    const checked = selected.has(tile.dataset.key);
    tile.classList.toggle("selected", checked);
    tile.querySelector(".photo-check").textContent = checked ? "✓" : "";
    if (tile.getAttribute("role") === "checkbox")
      tile.setAttribute("aria-checked", String(checked));
  });
}
function selectAllVisible() {
  const all = view.items.every((item) => selected.has(keyOf(item)));
  if (all) selected.clear();
  else view.items.forEach((item) => selected.add(keyOf(item)));
  if (view.type === "album" && !view.candidates) {
    selectMode = true;
    const b = $("album-select");
    if (b) b.textContent = "Done";
    renderGrid();
  } else syncSelectionTiles();
  renderSelection();
}
function renderSelection() {
  const items = selectionItems(),
    bar = $("selection-bar");
  bar.replaceChildren();
  bar.classList.toggle("hidden", !items.length);
  document.body.classList.toggle("has-selection", Boolean(items.length));
  if (!items.length) return;
  const summary = el("div", "selection-summary");
  summary.append(
    el("strong", "", plural(photoIds(items).size, "photo") + " selected"),
    button("Clear", "", () => {
      selected.clear();
      syncSelectionTiles();
      renderSelection();
    }),
  );
  const actions = el("div", "selection-actions");
  if (view.candidates) {
    const pet = petById(view.petId);
    actions.append(
      button(`Add to ${pet.name}`, "primary", () => assignSelection(pet.id)),
    );
    pets
      .filter((other) => other.id !== pet.id)
      .forEach((other) =>
        actions.append(button(other.name, "", () => assignSelection(other.id))),
      );
    actions.append(
      button("Not my pets", "subtle", () => assignSelection(null, true)),
    );
  } else {
    pets.forEach((pet, index) => {
      const b = button(pet.name, "", () => assignSelection(pet.id));
      b.title =
        (items[0].type === "photo" ? "Move to " : "Name as ") +
        pet.name +
        " (" +
        (index + 1) +
        ")";
      b.append(el("span", "selection-shortcut", String(index + 1)));
      actions.append(b);
    });
    actions.append(
      button(
        items[0].type === "photo" ? "Remove name" : "Not my pets",
        "subtle",
        () => assignSelection(null, true),
      ),
    );
  }
  bar.append(summary, actions);
  renderUndo();
}
function removeLocally(op) {
  op.startedAt = performance.now();
  boardGeneration++;
  lastSelected = -1;
  op.routeKey = view?.key;
  op.positions = op.items.map((item) => ({
    item,
    index: view.items.findIndex((current) => keyOf(current) === keyOf(item)),
  }));
  op.items.forEach((item) => {
    (item.type === "group" ? removedGroups : removedPhotos).add(item.id);
    selected.delete(keyOf(item));
  });
  view.items = view.items.filter(
    (item) => !op.items.some((removed) => keyOf(item) === keyOf(removed)),
  );
  view.offset = Math.max(0, view.offset - op.items.length);
  renderGrid();
  renderSelection();
  op.uiMs = performance.now() - op.startedAt;
}
function restoreLocally(op, failedIds) {
  if (!op) {
    if (view && ["album", "unidentified"].includes(view.type)) reloadPhotos();
    return;
  }
  lastSelected = -1;
  const restore = op.positions.filter(
    ({ item }) => !failedIds || photoFailed(item, failedIds),
  );
  restore.forEach(({ item }) =>
    (item.type === "group" ? removedGroups : removedPhotos).delete(item.id),
  );
  if (view?.key !== op.routeKey) return;
  restore.forEach(({ item, index }) => {
    if (!view.items.some((current) => keyOf(current) === keyOf(item)))
      view.items.splice(
        Math.min(Math.max(0, index), view.items.length),
        0,
        item,
      );
  });
  renderGrid();
  renderSelection();
}
async function assignSelection(petId, skip = false) {
  const items = selectionItems();
  if (!items.length) return;
  if (
    items[0].type === "group" &&
    items.reduce((count, item) => count + item.crop_count, 0) > 5000
  ) {
    toast("Choose up to 5,000 photos at a time. Your selection is still here.");
    return;
  }
  const groups = items[0].type === "group",
    name = petById(petId)?.name;
  const body = groups
    ? { group_ids: items.map((item) => item.id), pet_id: petId, skip }
    : {
        items: items.flatMap((item) =>
          assignmentsFor(item).map((id) => ({
            detection_id: id,
            pet_id: petId,
            status: skip ? "pending" : "confirmed",
            expected_pet_id: item.pet_id,
            expected_status: item.status,
          })),
        ),
      };
  const op = {
    kind: groups ? "groups" : "photos",
    path: groups ? "groups/batch" : "review/batch",
    body,
    items,
    label: skip
      ? groups
        ? "Photos hidden"
        : "Names removed"
      : `Added to ${name}`,
    petId,
    skip,
    count: photoIds(items).size,
  };
  op.body.operation_id = operationId();
  removeLocally(op);
  outbox.enqueue(op);
}
const outbox = new window.PetActionQueue({
  send: async (op) => {
    let result;
    if (op.body.items && op.body.items.length > 100) {
      const results = [];
      for (let offset = 0; offset < op.body.items.length; offset += 100) {
        try {
          const part = await api(op.path, {
            method: "POST",
            body: JSON.stringify({
              items: op.body.items.slice(offset, offset + 100),
              operation_id:
                (op.body.operation_id ||
                  (op.body.operation_id = operationId())) +
                "-" +
                offset,
            }),
          });
          results.push(...part.results);
        } catch (error) {
          error.failedIds = new Set([
            ...results.filter((row) => !row.saved).map((row) => row.id),
            ...op.body.items.slice(offset).map((item) => item.detection_id),
          ]);
          throw error;
        }
      }
      result = { results, failed: results.filter((row) => !row.saved).length };
    } else
      result = await api(op.path, {
        method: "POST",
        body: JSON.stringify(op.body),
      });
    if (result.failed) {
      const error = new Error(
        `${result.failed} photos could not be changed. Your other changes were saved.`,
      );
      error.failedIds = new Set(
        result.results.filter((row) => !row.saved).map((row) => row.id),
      );
      throw error;
    }
    return result;
  },
  onChange: () => {
    renderActivity();
  },
  onSuccess: (op, result) => {
    if (op.startedAt)
      console.info(
        "Pet action saved " +
          JSON.stringify({
            uiMs: Math.round(op.uiMs || 0),
            saveMs: Math.round(performance.now() - op.startedAt),
            photos: result.photos ?? op.count,
          }),
      );
    boardGeneration++;
    op.items.forEach((item) =>
      (item.type === "group" ? removedGroups : removedPhotos).delete(item.id),
    );
    if (op.kind === "photos") {
      dismissedUndoId = status.undo_group?.id || dismissedUndoId;
      sessionStorage.setItem("pet-dismissed-undo", dismissedUndoId);
    }
    if (op.kind === "undo") {
      if (op.path.startsWith("groups/undo/")) {
        dismissedUndoId = op.path.split("/").at(-1);
        sessionStorage.setItem("pet-dismissed-undo", dismissedUndoId);
      }
      lastUndo = null;
      status.undo_group = null;
      restoreLocally(op.original);
      toast("Change undone");
    } else {
      if (op.kind === "groups") {
        if (result.undo_id)
          lastUndo = {
            kind: "group",
            id: result.undo_id,
            label: op.label,
            original: op,
          };
      } else lastUndo = { kind: "photos", label: op.label, original: op };
      toast(`${op.count ? plural(op.count, "photo") + " · " : ""}${op.label}`);
    }
    renderUndo();
    refreshAlbums().catch(() => {});
    // A new page can load immediately; no policy fit, status request, or full
    // library refresh belongs to this action's completion path.
    if (view?.key === op.routeKey && !view.items.length && !view.loading)
      loadItems(false).catch(showError);
  },
  onError: (op, error) => {
    boardGeneration++;
    if (error.failedIds)
      op.items
        .filter((item) => !photoFailed(item, error.failedIds))
        .forEach((item) =>
          (item.type === "group" ? removedGroups : removedPhotos).delete(
            item.id,
          ),
        );
    if (op.kind !== "undo") {
      restoreLocally(op, error.failedIds);
      if (error.failedIds) {
        op.items = op.items.filter((item) =>
          photoFailed(item, error.failedIds),
        );
        op.positions = op.positions.filter(({ item }) =>
          photoFailed(item, error.failedIds),
        );
        if (op.body.items)
          op.body.items = op.body.items.filter((item) =>
            error.failedIds.has(item.detection_id),
          );
        op.count = photoIds(op.items).size;
        op.body.operation_id = operationId();
      }
    }
    showError(error);
    renderUndo();
  },
});
function retryFailed(failure) {
  const op = failure.op;
  if (op.kind !== "undo") {
    if (view?.key === op.routeKey) removeLocally(op);
    else
      op.items.forEach((item) =>
        (item.type === "group" ? removedGroups : removedPhotos).add(item.id),
      );
  }
  outbox.retry(failure.id);
}
function renderUndo() {
  const bar = $("undo-bar"),
    failed = outbox.failed.at(-1);
  bar.classList.toggle("hidden", !lastUndo && !failed);
  if (!lastUndo && !failed) return;
  $("undo-label").textContent = failed
    ? "A change wasn’t saved"
    : lastUndo.label;
  $("undo-action").textContent = failed ? "Retry" : "Undo";
  $("undo-action").disabled = Boolean(outbox.pending);
}
$("undo-action").addEventListener("click", () => {
  const failed = outbox.failed.at(-1);
  if (failed) {
    retryFailed(failed);
    return;
  }
  if (!lastUndo || outbox.pending) return;
  const original = lastUndo.original;
  if (lastUndo.kind === "group")
    outbox.enqueue({
      kind: "undo",
      path: "groups/undo/" + lastUndo.id,
      body: { operation_id: operationId() },
      original,
      items: [],
      label: "Undo",
    });
  else {
    const body = {
      items: original.items.flatMap((item) =>
        assignmentsFor(item).map((id) => ({
          detection_id: id,
          pet_id: item.pet_id,
          status: item.status,
          expected_pet_id: original.petId || undefined,
          expected_status: original.skip ? "pending" : "confirmed",
        })),
      ),
    };
    outbox.enqueue({
      kind: "undo",
      path: "review/batch",
      body: { ...body, operation_id: operationId() },
      original,
      items: [],
      label: "Undo",
    });
  }
});
$("dismiss-undo").addEventListener("click", () => {
  if (outbox.failed.length) {
    navigate("activity");
    return;
  }
  dismissedUndoId = lastUndo?.id || status.undo_group?.id || "";
  sessionStorage.setItem("pet-dismissed-undo", dismissedUndoId);
  lastUndo = null;
  status.undo_group = null;
  renderUndo();
});
let viewerItems = [];
function openViewer(items, index) {
  viewerItems = items;
  viewerIndex = index;
  renderViewer();
  if (!$("viewer").open) $("viewer").showModal();
}
function renderViewer() {
  const row = viewerItems[viewerIndex];
  if (!row) return;
  $("viewer-name").textContent = row.name || "Photo";
  const img = $("viewer-image");
  const preview = cropCache.get(`photo:${row.id}:320`);
  if (preview) img.src = preview;
  else img.removeAttribute("src");
  img.alt = row.name || "Pet photo";
  scheduleCrop(img, row.id, 1280, "photo");
  $("viewer-previous").disabled = viewerIndex === 0;
  $("viewer-next").disabled = viewerIndex === viewerItems.length - 1;
  const actions = $("viewer-actions");
  actions.replaceChildren();
  actions.append(
    el(
      "span",
      "",
      row.identities?.map((identity) => identity.name).join(" · ") ||
        row.pet_name ||
        "Unidentified",
    ),
  );
  if (view?.type === "album" && !view.petId) {
    (row.identities || []).forEach((identity) =>
      actions.append(
        button("Open " + identity.name, "", () => {
          $("viewer").close();
          navigate("pet/" + identity.pet_id);
        }),
      ),
    );
    return;
  }
  pets.forEach((pet) =>
    actions.append(
      button(pet.name, row.pet_id === pet.id ? "active" : "", () =>
        changeViewerPhoto(row, pet.id),
      ),
    ),
  );
  if (row.status === "confirmed") {
    actions.append(
      button("Remove name", "quiet", () => changeViewerPhoto(row, null)),
    );
    actions.append(
      button("Use as album cover", "quiet", async () => {
        const pet = petById(row.pet_id),
          previous = pet.cover_id;
        pet.cover_id = row.id;
        renderNavigation();
        toast("Album cover updated");
        try {
          await api(`pets/${row.pet_id}/cover`, {
            method: "PUT",
            body: JSON.stringify({ detection_id: row.id }),
          });
        } catch (error) {
          pet.cover_id = previous;
          renderNavigation();
          throw error;
        }
      }),
    );
  }
}
function changeViewerPhoto(row, petId) {
  const op = {
    kind: "photos",
    path: "review/batch",
    body: {
      items: assignmentsFor(row).map((id) => ({
        detection_id: id,
        pet_id: petId,
        status: petId ? "confirmed" : "pending",
        expected_pet_id: row.pet_id,
        expected_status: row.status,
      })),
    },
    items: [{ ...row, type: "photo" }],
    positions: [
      {
        item: { ...row, type: "photo" },
        index: view?.items.findIndex((item) => item.id === row.id) ?? 0,
      },
    ],
    routeKey: view?.key,
    label: petId ? "Moved to " + petById(petId).name : "Name removed",
    skip: !petId,
    petId,
    count: 1,
  };
  if (view?.items.some((item) => item.id === row.id)) removeLocally(op);
  op.body.operation_id = operationId();
  $("viewer").close();
  outbox.enqueue(op);
}
$("viewer-close").addEventListener("click", () => $("viewer").close());
$("viewer-previous").addEventListener("click", () => {
  if (viewerIndex > 0) {
    viewerIndex--;
    renderViewer();
  }
});
$("viewer-next").addEventListener("click", () => {
  if (viewerIndex < viewerItems.length - 1) {
    viewerIndex++;
    renderViewer();
  }
});
async function openDetail(group) {
  const data = await api("groups/" + group.id);
  detailGroup = { ...data, type: "group" };
  detailSelected = new Set(data.members.map((row) => row.id));
  $("detail-title").textContent = plural(data.photo_count, "similar photo");
  $("detail-grid").replaceChildren();
  data.members.forEach((row) => {
    const tile = button(
      "",
      "photo-tile selected",
      () => {
        if (detailSelected.has(row.id)) detailSelected.delete(row.id);
        else detailSelected.add(row.id);
        renderDetailSelection();
      },
      `Include ${row.name}`,
    );
    tile.dataset.id = row.id;
    tile.setAttribute("role", "checkbox");
    tile.setAttribute("aria-checked", "true");
    tile.append(image(row.id, row.name), el("span", "photo-check", "✓"));
    $("detail-grid").append(tile);
  });
  $("detail-grid").classList.add("selecting");
  renderDetailSelection();
  $("group-detail").showModal();
}
function renderDetailSelection() {
  $("detail-count").textContent =
    plural(
      new Set(
        detailGroup.members
          .filter((row) => detailSelected.has(row.id))
          .map((row) => row.asset_id),
      ).size,
      "photo",
    ) + " selected";
  $("detail-toggle").textContent =
    detailSelected.size === detailGroup.members.length
      ? "Clear selection"
      : "Select all";
  $("detail-grid")
    .querySelectorAll(".photo-tile")
    .forEach((tile) => {
      const checked = detailSelected.has(tile.dataset.id);
      tile.classList.toggle("selected", checked);
      tile.setAttribute("aria-checked", String(checked));
      tile.querySelector(".photo-check").textContent = checked ? "✓" : "";
    });
  const actions = $("detail-actions");
  actions.replaceChildren();
  pets.forEach((pet) => {
    const b = button(
      "Add to " + pet.name,
      pet.id === view?.petId ? "primary" : "",
      () => submitDetail(pet.id),
    );
    b.disabled = !detailSelected.size;
    actions.append(b);
  });
}
function submitDetail(petId) {
  const group = detailGroup,
    excluded = group.members
      .filter((row) => !detailSelected.has(row.id))
      .map((row) => row.id);
  const item = view.items.find((row) => row.id === group.id) || group;
  const op = {
    kind: "groups",
    path: "groups/" + group.id + "/decision",
    body: { pet_id: petId, excluded, skip: false },
    items: [item],
    label: "Added to " + petById(petId).name,
    petId,
    count: new Set(
      group.members
        .filter((row) => detailSelected.has(row.id))
        .map((row) => row.asset_id),
    ).size,
  };
  removeLocally(op);
  op.body.operation_id = operationId();
  $("group-detail").close();
  outbox.enqueue(op);
}
$("detail-close").addEventListener("click", () => $("group-detail").close());
$("detail-toggle").addEventListener("click", () => {
  detailSelected =
    detailSelected.size === detailGroup.members.length
      ? new Set()
      : new Set(detailGroup.members.map((row) => row.id));
  renderDetailSelection();
});
function renderSettings() {
  $("view").replaceChildren();
  heading("Settings");
  const container = el("div", "settings-container");
  $("view").append(container);
  const sorting = el("section", "settings-section");
  sorting.append(el("h2", "", "Automatic sorting"));
  const row = el("div", "setting-row"),
    copy = el("div");
  copy.append(
    el("strong", "", "Sorting preference"),
    el("p", "", "Choose the balance between coverage and mistakes."),
  );
  const profile = el("select");
  profile.setAttribute("aria-label", "Sorting preference");
  [
    ["careful", "Fewer mistakes"],
    ["balanced", "Balanced"],
    ["broader", "Sort more"],
  ].forEach(([value, text]) => {
    const o = el("option", "", text);
    o.value = value;
    profile.append(o);
  });
  profile.value = status.matching?.profile || "balanced";
  profile.addEventListener("change", async () => {
    const value = profile.value;
    profile.disabled = true;
    try {
      await api("matching", {
        method: "PUT",
        body: JSON.stringify({ mode: "adaptive", profile: value }),
      });
      toast("Sorting preference saved");
    } catch (error) {
      showError(error);
    } finally {
      profile.disabled = false;
      refreshStatus().catch(() => {});
    }
  });
  row.append(copy, profile);
  sorting.append(row);
  pets.forEach((pet) => {
    const r = el("div", "setting-row"),
      label = el("div");
    label.append(
      image(pet.cover_id, "", "nav-avatar", 160),
      el("strong", "", pet.name),
    );
    const toggle = button("", "switch", async () => {
      const enabled = toggle.getAttribute("aria-checked") !== "true";
      toggle.setAttribute("aria-checked", String(enabled));
      try {
        await api("pets/" + pet.id + "/automation", {
          method: "PUT",
          body: JSON.stringify({ enabled }),
        });
        pet.auto_tag = enabled;
      } catch (error) {
        toggle.setAttribute("aria-checked", String(!enabled));
        showError(error);
      }
    });
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-label", "Automatic sorting for " + pet.name);
    toggle.setAttribute("aria-checked", String(pet.auto_tag));
    r.append(label, toggle);
    sorting.append(r);
  });
  container.append(sorting);
  const add = el("section", "settings-section");
  add.append(el("h2", "", "Add a pet"));
  const form = el("form", "settings-form"),
    label = el("label", "", "Name"),
    name = el("input");
  name.required = true;
  name.maxLength = 64;
  name.placeholder = "Pet’s name";
  name.autocomplete = "off";
  label.append(name);
  const speciesLabel = el("label", "", "Species"),
    species = el("select");
  [
    ["dog", "Dog"],
    ["cat", "Cat"],
  ].forEach(([value, text]) => {
    const o = el("option", "", text);
    o.value = value;
    species.append(o);
  });
  speciesLabel.append(species);
  const submit = el("button", "primary", "Add pet");
  submit.type = "submit";
  form.append(label, speciesLabel, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      await api("pets", {
        method: "POST",
        body: JSON.stringify({ name: name.value, species: species.value }),
      });
      await refreshStatus();
      toast("Pet added");
      renderSettings();
    } catch (error) {
      showError(error);
    } finally {
      submit.disabled = false;
    }
  });
  add.append(form);
  container.append(add);
  const activity = el("section", "settings-section");
  activity.append(
    el("h2", "", "Library"),
    link("Background activity →", "#activity", "text-button"),
  );
  const note = el(
    "p",
    "settings-note",
    "New photos are checked automatically. Naming and correcting photos helps future sorting.",
  );
  activity.append(note);
  container.append(activity);
  const connection = el("section", "settings-section");
  connection.append(el("h2", "", "Connection"));
  const dl = el("dl", "connection-grid");
  [
    [
      "Immich",
      status.immich_connected
        ? "Connected"
        : status.demo
          ? "Demo mode"
          : "Not connected",
    ],
    ["Recognition", status.provider || "—"],
    ["Detection model", status.detector || "—"],
    ["Identity model", status.recognizer || "—"],
  ].forEach(([key, value]) =>
    dl.append(el("dt", "", key), el("dd", "", value)),
  );
  connection.append(dl);
  if (status.auth_required)
    connection.append(
      button("Forget service token", "text-button", () => {
        token = "";
        sessionStorage.removeItem("pet-service-token");
        refreshStatus().catch(showError);
      }),
    );
  container.append(connection);
}
function renderActivityPage() {
  if (view?.type !== "activity") return;
  $("view").replaceChildren();
  heading(
    "Background activity",
    "You can keep browsing and naming photos while this runs.",
  );
  const container = el("div", "settings-container");
  $("view").append(container);
  const check = status.recheck || {},
    section = el("section", "settings-section"),
    line = el("div", "activity-detail");
  const active = ["queued", "preparing", "running"].includes(check.state);
  line.append(
    el(
      "strong",
      "",
      active
        ? "Organizing your library"
        : check.state === "failed"
          ? "Sorting needs attention"
          : "Library up to date",
    ),
    el("span", "", active && check.percent != null ? check.percent + "%" : ""),
  );
  section.append(line);
  if (active) {
    const progress = el("progress", "activity-progress");
    progress.max = 100;
    if (check.percent != null) progress.value = check.percent;
    section.append(progress);
  }
  section.append(
    el(
      "p",
      "settings-note",
      active
        ? `${number(check.completed)} of ${number(check.total)} checked.`
        : check.state === "failed"
          ? check.error
          : "Clear matches are added automatically. There is nothing you need to wait for here.",
    ),
  );
  if (status.review_summary?.pending_tag_changes)
    section.append(
      el(
        "p",
        "settings-note",
        plural(status.review_summary.pending_tag_changes, "tag change") +
          " syncing to Immich.",
      ),
    );
  if (outbox.pending)
    section.append(
      el("p", "settings-note", plural(outbox.pending, "change") + " saving…"),
    );
  outbox.failed.forEach((failure) => {
    const r = el("div", "setting-row");
    r.append(
      el("p", "error-text", failure.error.message),
      button("Retry", "secondary", () => retryFailed(failure)),
      button("Dismiss", "text-button", () => {
        outbox.discard(failure.id);
        renderActivityPage();
      }),
    );
    section.append(r);
  });
  const controls = el("div", "heading-actions");
  controls.style.marginTop = "20px";
  [
    ["Check for new photos", "sync"],
    ["Scan existing library", "sync?full=true"],
    ["Recheck names", "rematch"],
    ["Retry failures", "retry"],
  ].forEach(([text, path]) =>
    controls.append(
      button(text, "secondary small-button", async () => {
        await api(path, { method: "POST" });
        toast("Background work queued");
        refreshStatus().catch(() => {});
      }),
    ),
  );
  controls.style.flexWrap = "wrap";
  section.append(controls);
  container.append(section);
  const jobsSection = el("section", "settings-section");
  jobsSection.append(el("h2", "", "Recent processing"));
  const list = el("div", "job-list");
  jobsSection.append(list);
  container.append(jobsSection);
  const version = routeVersion;
  api("jobs")
    .then((jobs) => {
      if (version !== routeVersion || view.type !== "activity") return;
      jobs.forEach((job) => {
        const row = el("div", "job-row"),
          name = el("div", "", job.name);
        if (job.error) name.append(el("small", "", job.error));
        row.append(name, el("span", "job-state " + job.status, job.status));
        list.append(row);
      });
    })
    .catch(() => {});
}
$("activity-indicator").addEventListener("click", () => navigate("activity"));
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("service-token").value;
  try {
    await refreshStatus();
    sessionStorage.setItem("pet-service-token", token);
    $("service-token").value = "";
    $("login-dialog").close();
    await openRoute();
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});
$("login-dialog").addEventListener("cancel", (event) => event.preventDefault());
window.addEventListener("hashchange", () => openRoute().catch(showError));
window.addEventListener("beforeunload", (event) => {
  if (outbox.pending) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("keydown", (event) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName))
    return;
  if ($("viewer").open) {
    if (event.key === "ArrowLeft") $("viewer-previous").click();
    if (event.key === "ArrowRight") $("viewer-next").click();
    return;
  }
  if ($("group-detail").open || $("login-dialog").open) return;
  if (event.key === "Escape") {
    selected.clear();
    syncSelectionTiles();
    renderSelection();
  }
  if (/^[1-9]$/.test(event.key) && selected.size) {
    const pet = pets[Number(event.key) - 1];
    if (pet) {
      event.preventDefault();
      assignSelection(pet.id);
    }
  }
});
(async () => {
  try {
    pets = await api("collections");
    renderNavigation();
    await openRoute();
    refreshStatus().catch(() => {});
  } catch (error) {
    if (!$("login-dialog").open) showError(error);
  }
})();
setInterval(() => {
  if (!document.hidden && !$("login-dialog").open)
    refreshStatus().catch(() => {});
}, 5000);
