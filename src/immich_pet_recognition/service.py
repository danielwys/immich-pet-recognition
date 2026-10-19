import hashlib
import io
import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from PIL import Image, ImageOps

from .config import Settings
from .immich import ImmichClient, ImmichError, asset_revision
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

    def start(self):
        self.store.execute("UPDATE jobs SET status='queued' WHERE status='processing'")
        if self.settings.demo:
            from .demo import seed_demo

            seed_demo(self)
            if self.suggestions_pending():
                self.rematch()
        elif self.settings.background:
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

    def run(self):
        next_poll = 0.0
        while not self.stop_event.is_set():
            try:
                if self.publish_one():
                    continue
                if self.rematch_requested.is_set():
                    self.rematch_requested.clear()
                    self.rematch()
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
            with self.model_lock:
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
            self.evaluate(detection)

    def embed(self, detection):
        vector = self.store.embedding(detection["id"], self.settings.recognizer_key)
        if vector is None:
            if self.settings.demo:
                raise ValueError("Demo embeddings are unavailable for this crop")
            with Image.open(self.crops / detection["crop"]) as crop:
                vector = self.recognizer.embed(crop)
            self.store.save_embedding(detection["id"], self.settings.recognizer_key, vector)
        return vector

    def references(self, cached_only=False):
        gallery = []
        for pet in self.store.rows("SELECT * FROM pets ORDER BY name"):
            pet["positive"], pet["negative"] = [], []
            for reference in self.store.rows(
                "SELECT d.*,r.kind FROM reference_images r "
                "JOIN detections d ON d.id=r.detection_id "
                "WHERE r.pet_id=?",
                (pet["id"],),
            ):
                vector = (
                    self.store.embedding(reference["id"], self.settings.recognizer_key)
                    if cached_only
                    else self.embed(reference)
                )
                if vector is not None:
                    pet[reference["kind"]].append(vector)
            gallery.append(pet)
        return gallery

    def evaluate(self, detection, gallery=None):
        model = self.settings.recognizer_key
        existing = self.store.one(
            "SELECT * FROM reviews WHERE detection_id=? AND model=?", (detection["id"], model)
        )
        if existing and existing["status"] != "pending":
            return
        result = match_pet(
            self.embed(detection),
            detection["species"],
            self.references() if gallery is None else gallery,
            self.settings.match_threshold,
            self.settings.match_margin,
            self.settings.min_references,
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
            if automatic:
                conn.execute(
                    "INSERT OR IGNORE INTO automatic_reviews VALUES (?,?)",
                    (detection["id"], model),
                )

    def automation_enabled(self, pet_id):
        if not pet_id:
            return False
        row = self.store.one("SELECT enabled FROM pet_automation WHERE pet_id=?", (pet_id,))
        return bool(row["enabled"]) if row else self.settings.auto_tag

    def pets(self):
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
                "sum(r.status='pending' AND r.score>=? AND r.margin>=?) strong_count,"
                "sum(r.status='confirmed' AND r.published=1) tagged_count,"
                "sum(r.status='confirmed' AND r.published=0) queued_count,"
                "sum(r.status='confirmed' AND r.published=1 AND ar.detection_id IS NOT NULL) "
                "auto_tagged_count FROM reviews r JOIN detections d ON d.id=r.detection_id "
                "JOIN assets a ON a.id=d.asset_id LEFT JOIN automatic_reviews ar "
                "ON ar.detection_id=r.detection_id AND ar.model=r.model "
                "WHERE r.model=? AND d.detector=? AND d.revision=a.revision GROUP BY r.pet_id",
                (
                    self.settings.match_threshold,
                    self.settings.match_margin,
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
            pet["example_gate_met"] = pet["positive_count"] >= self.settings.min_references
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
            # Persisted revision makes an interrupted backlog recheck resume on restart.
            self.gallery_changed()
        return next(pet for pet in self.pets() if pet["id"] == pet_id)

    def rematch(self):
        with self.model_lock:
            revision = self.store.state("gallery_revision", "0")
            gallery = self.references()
        for detection in self.store.rows(
            "SELECT d.* FROM detections d JOIN assets a ON a.id=d.asset_id "
            "JOIN reviews r ON r.detection_id=d.id AND r.model=? "
            "WHERE d.revision=a.revision AND d.detector=? AND r.status='pending'",
            (self.settings.recognizer_key, self.settings.detector_key),
        ):
            with self.model_lock:
                if self.stop_event.is_set():
                    return
                if revision != self.store.state("gallery_revision", "0"):
                    self.rematch_requested.set()
                    return
                self.embed(detection)
                self.evaluate(detection, gallery)
        self.store.set_state("matched_gallery_revision", revision)

    def suggestions_pending(self):
        return self.store.state("gallery_revision", "0") != self.store.state(
            "matched_gallery_revision", "0"
        )

    def gallery_changed(self):
        with self.store.connection() as conn:
            conn.execute(
                "INSERT INTO state VALUES ('gallery_revision','1') ON CONFLICT(key) "
                "DO UPDATE SET value=CAST(value AS INTEGER)+1"
            )
        self.rematch_requested.set()
        self.wake_event.set()
        if self.settings.demo:
            self.rematch_requested.clear()
            self.rematch()

    def recommendations(self, rows):
        """Fresh visible suggestions from cached vectors, without GPU work or tag writes."""
        gallery = self.references(cached_only=True)
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
            )
            if match.pet_id is None:
                continue
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
                        "required": self.settings.min_references,
                        "passed": match.references >= self.settings.min_references,
                    },
                    {
                        "label": "Similarity",
                        "value": match.score,
                        "required": self.settings.match_threshold,
                        "passed": match.score >= self.settings.match_threshold,
                    },
                    {
                        "label": "Lead over look-alikes",
                        "value": match.margin,
                        "required": self.settings.match_margin,
                        "passed": match.margin >= self.settings.match_margin,
                    },
                ],
            }
        return rows

    def add_pet(self, name, species):
        pet = {"id": str(uuid.uuid4()), "name": name, "species": species, "tag": f"Pets/{name}"}
        self.store.execute("INSERT INTO pets VALUES (:id,:name,:species,:tag)", pet)
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

    def decide(self, detection_id, pet_id, status, learn=False, expected_pet_id=None):
        with self.model_lock:
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

    def bulk_decide(self, items):
        results = []
        # Publication cannot race the batch; individual invalid rows remain untouched.
        with self.model_lock:
            for item in items:
                try:
                    self.decide(
                        item["detection_id"],
                        item["pet_id"],
                        item["status"],
                        item["learn"],
                        item.get("expected_pet_id"),
                    )
                    results.append({"id": item["detection_id"], "saved": True})
                except ValueError as error:
                    results.append(
                        {"id": item["detection_id"], "saved": False, "error": str(error)}
                    )
        return results

    def publish_one(self):
        with self.model_lock:
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
        with self.store.connection() as conn:
            conn.execute(
                "DELETE FROM tag_removals WHERE detection_id=? AND model=?",
                (row["detection_id"], row["model"]),
            )
            conn.execute(
                "UPDATE reviews SET published=?,error=NULL WHERE detection_id=? AND model=?",
                (published, row["detection_id"], row["model"]),
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
            self.store.execute(
                "UPDATE reviews SET error=? WHERE detection_id=? AND model=?",
                (self.safe_error(error), row["detection_id"], row["model"]),
            )
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
            # Refresh under the decision lock: a rejection must win over queued publication.
            current = self.store.one(
                "SELECT status FROM reviews WHERE detection_id=? AND model=?",
                (row["detection_id"], row["model"]),
            )
            if current["status"] != "confirmed":
                return True
            try:
                asset = self.immich.asset(row["asset_id"])
                if not eligible(asset) or asset_revision(asset) != row["revision"]:
                    raise ValueError("Asset changed or is unavailable; resync before publishing")
                if self.stop_event.is_set():
                    return False
                self.immich.tag(row["asset_id"], row["tag"])
                self._apply_tag_removals(row, asset)
                self._finish_tag_change(row, 1)
            except Exception as error:
                self.store.execute(
                    "UPDATE reviews SET error=? WHERE detection_id=? AND model=?",
                    (self.safe_error(error), row["detection_id"], row["model"]),
                )
        return True

    def retry_failed(self):
        self.store.execute(
            "UPDATE jobs SET status='queued',attempts=0,available_at=0,error=NULL "
            "WHERE status='failed' AND pipeline=?",
            (self.settings.pipeline_key,),
        )
        self.store.execute("UPDATE reviews SET error=NULL WHERE published=0")
        self.wake_event.set()

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
            "suggestions_updating": self.suggestions_pending(),
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
