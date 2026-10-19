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
        elif self.settings.background:
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
                if self.sync_requested.is_set() or time.monotonic() >= next_poll:
                    self.sync_requested.clear()
                    full = self.backfill_requested.is_set()
                    self.backfill_requested.clear()
                    self.sync(full=full)
                    next_poll = time.monotonic() + self.settings.poll_seconds
                if self.rematch_requested.is_set():
                    self.rematch_requested.clear()
                    self.rematch()
                if self.publish_one():
                    continue
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

    def references(self):
        gallery = []
        for pet in self.store.rows("SELECT * FROM pets ORDER BY name"):
            pet["positive"], pet["negative"] = [], []
            for reference in self.store.rows(
                "SELECT d.*,r.kind FROM reference_images r "
                "JOIN detections d ON d.id=r.detection_id "
                "WHERE r.pet_id=?",
                (pet["id"],),
            ):
                pet[reference["kind"]].append(self.embed(reference))
            gallery.append(pet)
        return gallery

    def evaluate(self, detection):
        model = self.settings.recognizer_key
        existing = self.store.one(
            "SELECT * FROM reviews WHERE detection_id=? AND model=?", (detection["id"], model)
        )
        if existing and existing["status"] != "pending":
            return
        result = match_pet(
            self.embed(detection),
            detection["species"],
            self.references(),
            self.settings.match_threshold,
            self.settings.match_margin,
            self.settings.min_references,
        )
        status = "confirmed" if self.settings.auto_tag and result.confident else "pending"
        self.store.execute(
            "INSERT INTO reviews(detection_id,model,pet_id,score,margin,status) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(detection_id,model) DO UPDATE SET "
            "pet_id=excluded.pet_id,score=excluded.score,"
            "margin=excluded.margin,status=excluded.status",
            (detection["id"], model, result.pet_id, result.score, result.margin, status),
        )

    def rematch(self):
        with self.model_lock:
            for detection in self.store.rows(
                "SELECT d.* FROM detections d JOIN assets a ON a.id=d.asset_id "
                "WHERE d.revision=a.revision AND d.detector=?",
                (self.settings.detector_key,),
            ):
                if self.stop_event.is_set():
                    return
                self.embed(detection)
                self.evaluate(detection)

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
        self.rematch_requested.set()
        self.wake_event.set()

    def decide(self, detection_id, pet_id, status):
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
                if pet is None or pet["species"] != detection["species"]:
                    raise ValueError("Choose a pet with the same species")
            row = self.store.one(
                "SELECT * FROM reviews WHERE detection_id=? AND model=?",
                (detection_id, self.settings.recognizer_key),
            )
            if row is None:
                raise ValueError("This detection is not ready for review")
            if row["published"]:
                raise ValueError(
                    "Already published; correct this tag in Immich before changing review"
                )
            self.store.execute(
                "UPDATE reviews SET pet_id=?,status=?,error=NULL WHERE detection_id=? AND model=?",
                (pet_id, status, detection_id, self.settings.recognizer_key),
            )
        self.wake_event.set()

    def publish_one(self):
        with self.model_lock:
            return self._publish_one()

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
                self.store.execute(
                    "UPDATE reviews SET published=1,error=NULL WHERE detection_id=? AND model=?",
                    (row["detection_id"], row["model"]),
                )
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
        self.store.execute("UPDATE reviews SET error=NULL WHERE status='confirmed' AND published=0")
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
            "auto_tag": self.settings.auto_tag,
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
