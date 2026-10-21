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

    def update_pet(self, pet_id, name, species):
        with self.model_lock:
            old = self.store.one("SELECT * FROM pets WHERE id=?", (pet_id,))
            if old is None:
                raise ValueError("Pet not found")
            pet = {"id": pet_id, "name": name, "species": species, "tag": f"Pets/{name}"}
            if pet == old:
                return pet
            with self.store.connection() as conn:
                # Check uniqueness before changing photos or queuing tag work.
                conn.execute(
                    "UPDATE pets SET name=:name,species=:species,tag=:tag WHERE id=:id", pet
                )
                if old["tag"] != pet["tag"]:
                    if not self.settings.demo:
                        conn.execute(
                            "INSERT OR IGNORE INTO tag_removals "
                            "SELECT r.detection_id,r.model,d.asset_id,? FROM reviews r "
                            "JOIN detections d ON d.id=r.detection_id "
                            "JOIN assets a ON a.id=d.asset_id WHERE r.pet_id=? "
                            "AND r.status='confirmed' AND r.model=? AND d.detector=? "
                            "AND d.revision=a.revision",
                            (
                                old["tag"],
                                pet_id,
                                self.settings.recognizer_key,
                                self.settings.detector_key,
                            ),
                        )
                    conn.execute(
                        "UPDATE reviews SET published=0,error=NULL WHERE pet_id=? "
                        "AND status='confirmed' AND model=? AND detection_id IN "
                        "(SELECT d.id FROM detections d JOIN assets a ON a.id=d.asset_id "
                        "WHERE d.detector=? AND d.revision=a.revision)",
                        (pet_id, self.settings.recognizer_key, self.settings.detector_key),
                    )
                if old["species"] != species:
                    corrected = json.dumps(
                        [
                            row[0]
                            for row in conn.execute(
                                "SELECT detection_id FROM reference_images "
                                "WHERE pet_id=? AND kind='positive' UNION "
                                "SELECT detection_id FROM reviews WHERE pet_id=? "
                                "AND status='confirmed' AND model=?",
                                (pet_id, pet_id, self.settings.recognizer_key),
                            )
                        ]
                    )
                    conn.execute(
                        "UPDATE detections SET species=? WHERE id IN "
                        "(SELECT value FROM json_each(?))",
                        (species, corrected),
                    )
                    # Keep feedback consistent with the corrected species.
                    conn.execute(
                        "DELETE FROM reference_images WHERE (pet_id=? OR detection_id IN "
                        "(SELECT value FROM json_each(?))) AND EXISTS "
                        "(SELECT 1 FROM detections d JOIN pets p "
                        "ON p.id=reference_images.pet_id WHERE d.id=detection_id "
                        "AND d.species<>p.species)",
                        (pet_id, corrected),
                    )
                    saved = conn.execute(
                        "SELECT value FROM state WHERE key='last_group_action'"
                    ).fetchone()
                    action = json.loads(saved[0]) if saved else None
                    if action:
                        related = {action.get("pet_id")}
                        for row in action["rows"]:
                            related.add(row.get("pet_id"))
                            for key in ("references", "after_references"):
                                related.update(ref["pet_id"] for ref in row.get(key, []))
                        if pet_id in related:
                            # A later species edit must not be undone by an older naming action.
                            conn.execute(
                                "UPDATE state SET value='null' WHERE key='last_group_action'"
                            )
                self._review_changed(conn)
            self.gallery_changed()
            return pet

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
            tracked = {
                member["id"] for _, group in snapshots.values() for member in group["members"]
            }
            unseen = [row for row in rows if row["id"] not in tracked]
            for row in unseen:
                row["vector"] = np.frombuffer(row["vector"], dtype=np.float32)
            fresh = similar_groups(unseen)
            self._remember_selections(
                [
                    {"id": group["id"], "member_ids": sorted(row["id"] for row in group["members"])}
                    for group in fresh
                ]
            )
            for group in fresh:
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

    def _remember_selections(self, groups):
        if not groups:
            return
        # Only photo IDs are saved, never old identities or scores. A page can
        # submit exactly what it showed even after a worker or service restart.
        with self.store.connection() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO review_selections VALUES (?,?,?,?)",
                [
                    (
                        group["id"],
                        self.settings.recognizer_key,
                        self.settings.detector_key,
                        json.dumps(sorted(group["member_ids"])),
                    )
                    for group in groups
                ],
            )

    def restore_selections(self, pipeline, groups):
        if pipeline != self.settings.pipeline_key:
            raise ValueError("These photos were analyzed by a different model")
        if sum(len(group["member_ids"]) for group in groups) > 5000:
            raise ValueError("Restore up to 5,000 photo selections at a time")
        for group in groups:
            ids = sorted(group["member_ids"])
            if (
                len(set(ids)) != len(ids)
                or hashlib.sha256("|".join(ids).encode()).hexdigest()[:24] != group["id"]
            ):
                raise ValueError("The selection does not match its photo IDs")
        self._remember_selections(groups)
        return {"restored": len(groups)}

    def _selection_ids(self, group_id):
        with self.group_lock:
            if self.group_snapshots.get("pipeline") == (
                self.settings.recognizer_key,
                self.settings.detector_key,
            ):
                entry = self.group_snapshots.get("items", {}).get(group_id)
                if entry is not None:
                    return {row["id"] for row in entry[1]["members"]}
        saved = self.store.one(
            "SELECT members FROM review_selections WHERE id=? AND model=? AND detector=?",
            (group_id, self.settings.recognizer_key, self.settings.detector_key),
        )
        return set(json.loads(saved["members"])) if saved else None

    def group(self, group_id):
        ids = self._selection_ids(group_id)
        if ids is None:
            # Compatibility with a page served before durable selections existed.
            self.groups()
            ids = self._selection_ids(group_id)
        if ids is None:
            return None
        members = self.store.rows(
            "SELECT d.id,d.asset_id,d.species,a.name,r.pet_id,r.score,r.margin "
            "FROM reviews r JOIN detections d ON d.id=r.detection_id "
            "JOIN assets a ON a.id=d.asset_id WHERE d.id IN "
            "(SELECT value FROM json_each(?)) AND r.model=? AND d.detector=? "
            "AND d.revision=a.revision AND r.status='pending' ORDER BY r.score DESC,d.id",
            (json.dumps(sorted(ids)), self.settings.recognizer_key, self.settings.detector_key),
        )
        if not members:
            return None
        choices = {}
        for member in members:
            choices[member["pet_id"]] = choices.get(member["pet_id"], 0) + 1
        pet_id = max(choices, key=choices.get)
        pet = self.store.one("SELECT name FROM pets WHERE id=?", (pet_id,))
        indexes = sorted(set([0, len(members) // 3, 2 * len(members) // 3, len(members) - 1]))
        return {
            "id": group_id,
            "pet_id": pet_id,
            "pet_name": pet["name"] if pet else None,
            "species": members[0]["species"],
            "members": members,
            "crop_count": len(members),
            "photo_count": len({row["asset_id"] for row in members}),
            "asset_ids": sorted({row["asset_id"] for row in members}),
            "samples": [members[index] for index in indexes],
        }

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
        rebuilt = False
        for group_id in group_ids:
            members = self._selection_ids(group_id)
            if members is None and not rebuilt:
                self.groups()
                rebuilt = True
                members = self._selection_ids(group_id)
            if members is None:
                raise ValueError(
                    "This selection could not be found. Reload the photos before naming it."
                )
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
                            "rejected" if skip else "confirmed",
                            detection_id,
                            self.settings.recognizer_key,
                        ),
                    )
                    if skip:
                        conn.execute(
                            "INSERT INTO reference_images SELECT id,?,'negative' FROM pets "
                            "WHERE species=? ON CONFLICT(pet_id,detection_id) "
                            "DO UPDATE SET kind='negative'",
                            (detection_id, row["species"]),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM reference_images WHERE detection_id=? "
                            "AND kind='positive' AND pet_id<>?",
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
                                "WHERE id=? AND species=? ON CONFLICT(pet_id,detection_id) "
                                "DO UPDATE SET kind='negative'",
                                (detection_id, row["pet_id"], pet["species"]),
                            )
                    row["after_references"] = [
                        dict(ref)
                        for ref in conn.execute(
                            "SELECT pet_id,kind FROM reference_images WHERE detection_id=? "
                            "ORDER BY pet_id,kind",
                            (detection_id,),
                        )
                    ]
                self._review_changed(conn)
            self.gallery_changed()
            action = {
                "id": uuid.uuid4().hex,
                "pet_id": pet_id,
                "skip": skip,
                "rows": before,
                "revision": self.store.state("gallery_revision", "0"),
                "created_at": datetime.now(UTC).isoformat(),
            }
            self.store.set_state("last_group_action", json.dumps(action))
            return {
                "saved": len(before),
                "skipped": len(ids) - len(before),
                "photos": len({row["asset_id"] for row in before}),
                "undo_id": action["id"],
            }

    def undo_info(self):
        action = json.loads(self.store.state("last_group_action", "null"))
        if (
            not action
            or (datetime.now(UTC) - datetime.fromisoformat(action["created_at"])).total_seconds()
            > 3600
        ):
            return None
        return {
            "id": action["id"],
            "count": len(action["rows"]),
            "pet_id": action["pet_id"],
            "skip": action["skip"],
        }

    def undo_group(self, action_id):
        with self.model_lock:
            info = self.undo_info()
            if info is None or info["id"] != action_id:
                raise ValueError(
                    "This undo is no longer available. Correct tags in the pet collection."
                )
            action = json.loads(self.store.state("last_group_action"))
            selected = json.dumps([row["detection_id"] for row in action["rows"]])
            with self.store.connection() as conn:
                current = {
                    row["detection_id"]: dict(row)
                    for row in conn.execute(
                        "SELECT r.* FROM reviews r JOIN detections d ON d.id=r.detection_id "
                        "JOIN assets a ON a.id=d.asset_id WHERE d.id IN "
                        "(SELECT value FROM json_each(?)) AND r.model=? "
                        "AND d.detector=? AND d.revision=a.revision",
                        (selected, self.settings.recognizer_key, self.settings.detector_key),
                    )
                }
                references = {}
                for ref in conn.execute(
                    "SELECT * FROM reference_images WHERE detection_id IN "
                    "(SELECT value FROM json_each(?)) ORDER BY pet_id,kind",
                    (selected,),
                ):
                    references.setdefault(ref["detection_id"], []).append(
                        {"pet_id": ref["pet_id"], "kind": ref["kind"]}
                    )
                for old in action["rows"]:
                    row = current.get(old["detection_id"])
                    if (
                        not row
                        or row["status"] != ("rejected" if action["skip"] else "confirmed")
                        or row["pet_id"] != (None if action["skip"] else action["pet_id"])
                        or references.get(old["detection_id"], []) != old["after_references"]
                    ):
                        raise ValueError(
                            "A photo in this group changed. Correct it in the pet collection."
                        )
                # Only automatic assignments still owned by this teaching pass
                # are reverted. A later explicit name has removed that marker.
                propagated = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT r.*,d.asset_id FROM reviews r JOIN detections d "
                        "ON d.id=r.detection_id "
                        "JOIN assets a ON a.id=d.asset_id JOIN automatic_sources src "
                        "ON src.detection_id=r.detection_id AND src.model=r.model "
                        "JOIN automatic_reviews ar ON ar.detection_id=r.detection_id "
                        "AND ar.model=r.model "
                        "WHERE src.revision=? AND r.model=? AND r.status='confirmed' "
                        "AND d.detector=? AND d.revision=a.revision",
                        (
                            action["revision"],
                            self.settings.recognizer_key,
                            self.settings.detector_key,
                        ),
                    )
                ]
                tags = {row["id"]: row["tag"] for row in conn.execute("SELECT id,tag FROM pets")}
                for old in [*action["rows"], *propagated]:
                    row = current[old["detection_id"]] if "references" in old else old
                    if row["status"] == "confirmed" and row["pet_id"] and not self.settings.demo:
                        # Required even for unpublished rows: an HTTP tag write
                        # may be in flight while the user presses Undo.
                        conn.execute(
                            "INSERT OR IGNORE INTO tag_removals VALUES (?,?,?,?)",
                            (
                                old["detection_id"],
                                self.settings.recognizer_key,
                                old["asset_id"],
                                tags[row["pet_id"]],
                            ),
                        )
                    conn.execute(
                        "DELETE FROM automatic_reviews WHERE detection_id=? AND model=?",
                        (old["detection_id"], self.settings.recognizer_key),
                    )
                    conn.execute(
                        "DELETE FROM automatic_sources WHERE detection_id=? AND model=?",
                        (old["detection_id"], self.settings.recognizer_key),
                    )
                    conn.execute(
                        "UPDATE reviews SET pet_id=?,status='pending',published=0,error=NULL,"
                        "score=?,margin=? "
                        "WHERE detection_id=? AND model=?",
                        (
                            old.get("pet_id") if "references" in old else None,
                            old["score"],
                            old["margin"],
                            old["detection_id"],
                            self.settings.recognizer_key,
                        ),
                    )
                    if "references" in old:
                        conn.execute(
                            "UPDATE detections SET species=? WHERE id=?",
                            (old["species"], old["detection_id"]),
                        )
                        conn.execute(
                            "DELETE FROM reference_images WHERE detection_id=?",
                            (old["detection_id"],),
                        )
                        conn.executemany(
                            "INSERT INTO reference_images VALUES (?,?,?)",
                            [
                                (ref["pet_id"], old["detection_id"], ref["kind"])
                                for ref in old["references"]
                            ],
                        )
                    elif row["pet_id"]:
                        conn.execute(
                            "DELETE FROM reference_images WHERE pet_id=? AND detection_id=? "
                            "AND kind='positive'",
                            (row["pet_id"], old["detection_id"]),
                        )
                self._review_changed(conn)
                conn.execute("INSERT OR REPLACE INTO state VALUES ('last_group_action','null')")
            self.gallery_changed()
            return {"restored": len(action["rows"]), "automatic_restored": len(propagated)}

    def publish_one(self):
        # Only the publisher is serialized across network calls. Human naming
        # remains available even when Immich is taking its full HTTP timeout.
        with self.publish_lock:
            return self._publish_one() or self._remove_one()

    def _apply_tag_removals(self, row, asset):
        for removal in self.store.rows(
            "SELECT tag FROM tag_removals WHERE detection_id=? AND model=?",
            (row["detection_id"], row["model"]),
        ):
            # Other animals in the same photo can still legitimately need this tag.
            needed = self.store.one(
                "SELECT 1 FROM reviews r JOIN detections d ON d.id=r.detection_id "
                "JOIN pets p ON p.id=r.pet_id WHERE d.asset_id=? AND d.revision=? "
                "AND d.detector=? AND r.model=? AND r.status='confirmed' AND p.tag=?",
                (
                    row["asset_id"],
                    row["revision"],
                    self.settings.detector_key,
                    row["model"],
                    removal["tag"],
                ),
            )
            if not needed:
                self.immich.untag(row["asset_id"], removal["tag"], asset)

    def _finish_tag_change(self, row, published):
        with self.model_lock:
            if not self._current_tag_operation(row):
                # A newer decision already queued the compensating removal or
                # publication. Do not clear its debt or mark it published.
                return
            with self.store.connection() as conn:
                conn.execute(
                    "DELETE FROM tag_removals WHERE detection_id=? AND model=?",
                    (row["detection_id"], row["model"]),
                )
                conn.execute(
                    "UPDATE reviews SET published=?,error=NULL WHERE detection_id=? AND model=?",
                    (published, row["detection_id"], row["model"]),
                )
                self._review_changed(conn)

    def _current_tag_operation(self, row):
        current = self.store.one(
            "SELECT r.pet_id,r.status,r.published,d.revision,p.tag FROM reviews r "
            "JOIN detections d ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
            "LEFT JOIN pets p ON p.id=r.pet_id "
            "WHERE r.detection_id=? AND r.model=? AND d.detector=? AND d.revision=a.revision",
            (row["detection_id"], row["model"], self.settings.detector_key),
        )
        return (
            current
            and all(
                current[key] == row[key] for key in ("pet_id", "status", "published", "revision")
            )
            and ("tag" not in row or current["tag"] == row["tag"])
        )

    def _tag_change_failed(self, row, error):
        with self.model_lock:
            if self._current_tag_operation(row):
                self.store.execute(
                    "UPDATE reviews SET error=? WHERE detection_id=? AND model=?",
                    (self.safe_error(error), row["detection_id"], row["model"]),
                )

    def _remove_one(self):
        if self.settings.demo:
            return False
        row = self.store.one(
            "SELECT r.*,d.asset_id,d.revision FROM reviews r "
            "JOIN detections d ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
            "WHERE r.status<>'confirmed' AND r.error IS NULL AND r.model=? "
            "AND d.detector=? AND d.revision=a.revision "
            "AND EXISTS(SELECT 1 FROM tag_removals t WHERE t.detection_id=r.detection_id "
            "AND t.model=r.model) LIMIT 1",
            (self.settings.recognizer_key, self.settings.detector_key),
        )
        if row is None:
            return False
        try:
            asset = self.immich.asset(row["asset_id"])
            if not eligible(asset) or asset_revision(asset) != row["revision"]:
                raise ValueError("Asset changed or is unavailable; resync before removing tags")
            if self.stop_event.is_set():
                return False
            self._apply_tag_removals(row, asset)
            self._finish_tag_change(row, 0)
        except Exception as error:
            self._tag_change_failed(row, error)
        return True

    def _publish_one(self):
        row = self.store.one(
            "SELECT r.*,d.asset_id,d.revision,p.tag FROM reviews r "
            "JOIN detections d ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
            "JOIN pets p ON p.id=r.pet_id WHERE r.status='confirmed' AND r.published=0 "
            "AND r.error IS NULL AND r.model=? AND d.detector=? AND d.revision=a.revision LIMIT 1",
            (self.settings.recognizer_key, self.settings.detector_key),
        )
        if row is None or self.settings.demo:
            return False
        with self.model_lock:
            if not self._current_tag_operation(row):
                return True
            self.active_publication = row
        try:
            asset = self.immich.asset(row["asset_id"])
            if not eligible(asset) or asset_revision(asset) != row["revision"]:
                raise ValueError("Asset changed or is unavailable; resync before publishing")
            if self.stop_event.is_set():
                return False
            with self.model_lock:
                if not self._current_tag_operation(row):
                    return True
            self.immich.tag(row["asset_id"], row["tag"])
            self._apply_tag_removals(row, asset)
            self._finish_tag_change(row, 1)
        except Exception as error:
            self._tag_change_failed(row, error)
        finally:
            with self.model_lock:
                self.active_publication = None
        return True

    def retry_failed(self):
        self.store.execute(
            "UPDATE jobs SET status='queued',attempts=0,available_at=0,error=NULL "
            "WHERE status='failed' AND pipeline=?",
            (self.settings.pipeline_key,),
        )
        self.store.execute("UPDATE reviews SET error=NULL WHERE published=0")
        if self.recheck_status()["state"] == "failed":
            self.gallery_changed()
        self.wake_event.set()

    def review_summary(self):
        """Count photos separately from crops, using the review page's tag semantics."""
        published = "status='confirmed'" if self.settings.demo else "published=1"
        current = (
            "WITH current AS MATERIALIZED (SELECT d.asset_id,r.* FROM detections d "
            "JOIN assets a ON a.id=d.asset_id JOIN reviews r ON r.detection_id=d.id "
            "WHERE d.revision=a.revision AND d.detector=? AND r.model=?),"
            f"tagged AS MATERIALIZED (SELECT DISTINCT asset_id FROM current WHERE {published} "
            "AND pet_id IS NOT NULL UNION SELECT tr.asset_id FROM tag_removals tr "
            "JOIN pets p ON p.tag=tr.tag) "
        )
        params = (self.settings.detector_key, self.settings.recognizer_key)
        summary = self.store.one(
            current + "SELECT coalesce(sum(NOT has_tag AND active>0),0) untagged_photos,"
            "coalesce(sum(CASE WHEN NOT has_tag THEN active ELSE 0 END),0) untagged_crops,"
            "coalesce(sum(completed>0),0) tagged_photos,"
            "coalesce(sum(completed),0) tagged_crops,"
            "coalesce(sum(has_tag AND pending>0),0) partially_tagged_photos,"
            "coalesce(sum(CASE WHEN has_tag THEN pending ELSE 0 END),0) extra_pending_crops,"
            "coalesce(sum(changes),0) pending_tag_changes FROM ("
            "SELECT c.asset_id,sum(c.status<>'rejected') active,"
            "sum(c.status='pending') pending,"
            f"sum(c.status='confirmed' AND ({published})) completed,"
            "EXISTS(SELECT 1 FROM tagged t WHERE t.asset_id=c.asset_id) has_tag,"
            "sum((c.status='confirmed' AND c.published=0 AND ?=0) OR EXISTS("
            "SELECT 1 FROM tag_removals tr WHERE tr.detection_id=c.detection_id "
            "AND tr.model=c.model)) changes FROM current c GROUP BY c.asset_id)",
            (*params, int(self.settings.demo)),
        )
        # Each pending crop has one first unmet check, so these categories never
        # double-count. Cached results are explicitly provisional during a recheck.
        summary["reasons"] = {
            row["reason"]: row["count"]
            for row in self.store.rows(
                current + "SELECT CASE WHEN c.pet_id IS NULL THEN 'no_candidate' "
                "WHEN coalesce(refs.count,0)<json_extract(rule.value,'$.min_references') "
                "THEN 'needs_examples' "
                "WHEN c.score<json_extract(rule.value,'$.threshold') THEN 'below_similarity' "
                "WHEN c.margin<json_extract(rule.value,'$.margin') THEN 'ambiguous' "
                "WHEN coalesce(pa.enabled,?)=0 THEN 'automation_paused' "
                "ELSE 'ready' END reason,count(*) count FROM current c "
                "LEFT JOIN (SELECT pet_id,count(*) count FROM reference_images "
                "WHERE kind='positive' GROUP BY pet_id) refs ON refs.pet_id=c.pet_id "
                "LEFT JOIN pet_automation pa ON pa.pet_id=c.pet_id "
                "LEFT JOIN json_each(?) rule ON rule.key=c.pet_id "
                "WHERE c.status='pending' AND NOT EXISTS("
                "SELECT 1 FROM tagged t WHERE t.asset_id=c.asset_id) GROUP BY reason",
                (
                    *params,
                    int(self.settings.auto_tag),
                    json.dumps(self.policies()),
                ),
            )
        }
        return summary

    def status(self):
        jobs = {
            r["status"]: r["count"]
            for r in self.store.rows(
                "SELECT status,count(*) count FROM jobs WHERE pipeline=? GROUP BY status",
                (self.settings.pipeline_key,),
            )
        }
        return {
            "demo": self.settings.demo,
            "auth_required": self.settings.auth_required and not self.settings.demo,
            "auto_tag": any(pet["auto_tag"] for pet in self.pets()),
            "provider": self.settings.provider,
            "detector": self.settings.detector_repo,
            "recognizer": self.settings.recognizer_repo,
            "jobs": jobs,
            "last_sync": self.store.state("last_sync"),
            "last_error": self.store.state("last_error", ""),
            "immich_connected": bool(self.store.state("last_sync"))
            and not self.store.state("last_error")
            and not self.settings.demo,
            "min_references": self.settings.min_references,
            "match_threshold": self.settings.match_threshold,
            "match_margin": self.settings.match_margin,
            "matching": self.matching_options(),
            "undo_group": self.undo_info(),
            "suggestions_updating": self.suggestions_pending(),
            "recheck": self.recheck_status(),
            "review_summary": self.review_summary(),
            "pending_tag_removals": self.store.one("SELECT count(*) total FROM tag_removals")[
                "total"
            ],
            "reviews": {
                r["status"]: r["count"]
                for r in self.store.rows(
                    "SELECT r.status,count(*) count FROM reviews r JOIN detections d "
                    "ON d.id=r.detection_id JOIN assets a ON a.id=d.asset_id "
                    "WHERE r.model=? AND d.detector=? AND d.revision=a.revision GROUP BY r.status",
                    (self.settings.recognizer_key, self.settings.detector_key),
                )
            },
        }
