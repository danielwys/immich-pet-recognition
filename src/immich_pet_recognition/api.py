import hashlib
import io
import json
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .immich import asset_revision
from .service import Service, eligible

STATIC = Path(__file__).parent / "static"


class PetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    species: Literal["dog", "cat"] = "dog"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or "/" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Use a pet name without slashes or control characters")
        return value


class ReferenceCreate(BaseModel):
    detection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["positive", "negative"] = "positive"


class CoverUpdate(BaseModel):
    detection_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class AutomationUpdate(BaseModel):
    enabled: bool


class MatchingUpdate(BaseModel):
    mode: Literal["adaptive", "fixed"] = "adaptive"
    profile: Literal["careful", "balanced", "broader"] = "balanced"


class WriteOperation(BaseModel):
    operation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class GroupDecision(WriteOperation):
    pet_id: UUID | None = None
    excluded: list[str] = Field(default_factory=list, max_length=200)
    skip: bool = False


class GroupBatchDecision(WriteOperation):
    group_ids: list[str] = Field(min_length=1, max_length=5000)
    pet_id: UUID | None = None
    skip: bool = False

    @field_validator("group_ids")
    @classmethod
    def unique_groups(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Each group can appear only once in a batch")
        return values


class ReviewSelection(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{24}$")
    member_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("member_ids")
    @classmethod
    def detection_ids(cls, values):
        if any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in values):
            raise ValueError("Use valid photo identifiers")
        return values


class ReviewSelections(BaseModel):
    pipeline: str = Field(min_length=1, max_length=512)
    groups: list[ReviewSelection] = Field(min_length=1, max_length=5000)


class Decision(BaseModel):
    status: Literal["confirmed", "rejected", "pending"]
    pet_id: UUID | None = None
    expected_pet_id: UUID | None = None
    expected_status: Literal["pending", "confirmed", "rejected"] | None = None


class BulkDecision(Decision):
    detection_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class BulkReview(WriteOperation):
    items: list[BulkDecision] = Field(min_length=1, max_length=100)

    @field_validator("items")
    @classmethod
    def unique_detections(cls, items):
        if len({item.detection_id for item in items}) != len(items):
            raise ValueError("Each photo can appear only once in a batch")
        return items


class ReferenceAssignment(ReferenceCreate):
    pet_id: UUID


class ReferenceBatch(BaseModel):
    items: list[ReferenceAssignment] = Field(min_length=1, max_length=100)


def create_app(settings: Settings | None = None, service: Service | None = None):
    settings = settings or Settings()
    service = service or Service(settings)

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        yield
        service.close()

    app = FastAPI(title="Immich Pet Recognition", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if settings.demo or not settings.auth_required:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.url.netloc}"
                if (origin is not None and origin != expected_origin) or request.headers.get(
                    "sec-fetch-site"
                ) == "cross-site":
                    raise HTTPException(403, "Use the companion page to make changes")
            return
        token = credentials.credentials if credentials else ""
        if not secrets.compare_digest(token, settings.service_token.get_secret_value()):
            raise HTTPException(
                401, "Enter the service token", headers={"WWW-Authenticate": "Bearer"}
            )

    auth = [Depends(authenticate)]
    asset_version = hashlib.sha256(
        b"".join(
            (STATIC / asset).read_bytes() for asset in ("app.js", "action-queue.js", "style.css")
        )
    ).hexdigest()[:12]
    index_html = (STATIC / "index.html").read_text()
    for asset in ("app.js", "action-queue.js", "style.css"):
        index_html = index_html.replace(f"/static/{asset}", f"/static/{asset}?v={asset_version}")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index():
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/status", dependencies=auth)
    def status():
        return service.status()

    @app.get("/api/pets", dependencies=auth)
    def pets():
        return service.pets()

    @app.get("/api/matching", dependencies=auth)
    def matching(
        mode: Literal["adaptive", "fixed"] | None = None,
        profile: Literal["careful", "balanced", "broader"] | None = None,
    ):
        return service.matching_preview(mode, profile)

    @app.put("/api/matching", dependencies=auth)
    def set_matching(body: MatchingUpdate):
        return service.set_matching_options(body.mode, body.profile)

    @app.get("/api/collections", dependencies=auth)
    def collections():
        pets = service.collections()
        for pet in pets:
            saved = service.store.state("pet_cover:" + pet["id"])
            if saved and current_cover(pet["id"], saved):
                pet["cover_id"] = saved
        return pets

    def current_cover(pet_id, detection_id):
        return service.store.one(
            "SELECT d.id FROM detections d JOIN assets a ON a.id=d.asset_id "
            "JOIN reviews r ON r.detection_id=d.id WHERE d.id=? AND r.pet_id=? "
            "AND r.status='confirmed' AND r.model=? AND d.detector=? AND d.revision=a.revision",
            (detection_id, pet_id, settings.recognizer_key, settings.detector_key),
        )

    @app.put("/api/pets/{pet_id}/cover", dependencies=auth)
    def set_cover(pet_id: UUID, body: CoverUpdate):
        with service.model_lock:
            if not current_cover(str(pet_id), body.detection_id):
                raise HTTPException(409, "Choose a current photo from this pet's album")
            service.store.set_state("pet_cover:" + str(pet_id), body.detection_id)
        return {"cover_id": body.detection_id}

    @app.get("/api/groups", dependencies=auth)
    def groups(
        pet_id: UUID | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(12, ge=1, le=48),
        near_match: bool = False,
    ):
        rows = service.groups()
        if pet_id:
            rows = [row for row in rows if row["pet_id"] == str(pet_id)]
            if near_match:
                rule = service.policies().get(str(pet_id), {})
                floor = max(0.35, rule.get("threshold", settings.match_threshold) - 0.075)

                def scores(group):
                    return [
                        member["score"]
                        for member in group["members"]
                        if member["pet_id"] == str(pet_id)
                    ]

                rows = [row for row in rows if max(scores(row), default=0) >= floor]
                rows.sort(
                    key=lambda row: (
                        -sum(scores(row)) / len(scores(row)),
                        -row["photo_count"],
                        row["id"],
                    )
                )
        return {
            "updating": service.suggestions_pending(),
            "items": [
                {k: v for k, v in row.items() if k != "members"}
                for row in rows[offset : offset + limit]
            ],
            "total_groups": len(rows),
            "total_crops": sum(row["crop_count"] for row in rows),
            "total_photos": len({member["asset_id"] for row in rows for member in row["members"]}),
        }

    def save_once(body, operation, scope=None):
        if not body.operation_id:
            return operation()
        payload = body.model_dump(mode="json", exclude={"operation_id"})
        fingerprint = hashlib.sha256(
            json.dumps([scope or type(body).__name__, payload], sort_keys=True).encode()
        ).hexdigest()
        with service.model_lock:
            receipt = service.store.one(
                "SELECT * FROM decision_receipts WHERE id=?", (body.operation_id,)
            )
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise HTTPException(409, "This request identifier was already used")
                result = json.loads(receipt["result"])
                if result.get("undo_id"):
                    current_undo = service.undo_info()
                    if not current_undo or current_undo["id"] != result["undo_id"]:
                        result["undo_id"] = None
                return result
            result = operation()
            with service.store.connection() as conn:
                conn.execute(
                    "INSERT INTO decision_receipts VALUES (?,?,?,?)",
                    (body.operation_id, fingerprint, json.dumps(result), time.time()),
                )
                conn.execute(
                    "DELETE FROM decision_receipts WHERE created<? OR id NOT IN "
                    "(SELECT id FROM decision_receipts ORDER BY created DESC LIMIT 256)",
                    (time.time() - 3600,),
                )
            return result

    @app.post("/api/groups/batch", dependencies=auth)
    def group_batch_decision(body: GroupBatchDecision):
        try:
            return save_once(
                body,
                lambda: service.decide_groups(
                    body.group_ids, str(body.pet_id) if body.pet_id else None, body.skip
                ),
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/groups/{group_id}", dependencies=auth)
    def group_detail(group_id: str):
        row = service.group(group_id)
        if not row:
            raise HTTPException(
                409, "These photos have already been handled or are no longer available."
            )
        return row

    @app.post("/api/groups/selections", dependencies=auth)
    def restore_selections(body: ReviewSelections):
        try:
            return service.restore_selections(
                body.pipeline, [group.model_dump() for group in body.groups]
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/api/groups/{group_id}/decision", dependencies=auth)
    def group_decision(group_id: str, body: GroupDecision):
        try:
            return save_once(
                body,
                lambda: service.decide_group(
                    group_id, str(body.pet_id) if body.pet_id else None, body.excluded, body.skip
                ),
                scope="group:" + group_id,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/api/groups/undo/{action_id}", dependencies=auth)
    def undo_group(action_id: str, body: WriteOperation | None = None):
        try:
            if body is None:
                return service.undo_group(action_id)
            return save_once(body, lambda: service.undo_group(action_id), scope="undo:" + action_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.put("/api/pets/{pet_id}/automation", dependencies=auth)
    def set_automation(pet_id: UUID, body: AutomationUpdate):
        try:
            return service.set_automation(str(pet_id), body.enabled)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None

    @app.post("/api/pets", dependencies=auth, status_code=201)
    def add_pet(body: PetCreate):
        try:
            pet = service.add_pet(body.name, body.species)
            service.set_automation(pet["id"], True)
            return pet
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A pet with this name already exists") from None

    @app.put("/api/pets/{pet_id}", dependencies=auth)
    def update_pet(pet_id: UUID, body: PetCreate):
        try:
            return service.update_pet(str(pet_id), body.name, body.species)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A pet with this name already exists") from None

    @app.post("/api/pets/{pet_id}/references", dependencies=auth, status_code=201)
    def add_reference(pet_id: UUID, body: ReferenceCreate):
        try:
            service.add_reference(str(pet_id), body.detection_id, body.kind)
            return {"status": "saved", "rematch": "queued"}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/references/batch", dependencies=auth)
    def bulk_references(body: ReferenceBatch):
        results = []
        with service.batch_updates():
            for item in body.items:
                try:
                    service.add_reference(str(item.pet_id), item.detection_id, item.kind)
                    results.append({"id": item.detection_id, "saved": True})
                except ValueError as error:
                    results.append({"id": item.detection_id, "saved": False, "error": str(error)})
        return {
            "results": results,
            "saved": sum(item["saved"] for item in results),
            "failed": sum(not item["saved"] for item in results),
        }

    @app.get("/api/detections", dependencies=auth)
    def detections(
        status: Literal["all", "pending", "confirmed", "rejected"] = "pending",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=48, ge=1, le=100),
        sort: Literal["suggested", "newest"] = "suggested",
        pet_id: UUID | None = None,
        source: Literal["all", "manual", "automatic"] = "all",
        tagging: Literal["all", "untagged", "unassigned", "tagged"] = "all",
        include_dismissed: bool = True,
        unique_photos: bool = False,
    ):
        clause = (
            "FROM detections d JOIN assets a ON a.id=d.asset_id "
            "JOIN reviews r ON r.detection_id=d.id LEFT JOIN pets p ON p.id=r.pet_id "
            "WHERE d.revision=a.revision AND d.detector=? AND r.model=?"
        )
        params = [settings.detector_key, settings.recognizer_key]
        published = "pr.status='confirmed'" if settings.demo else "pr.published=1"
        # A photo can contain several detected animals. One published pet tag makes
        # the whole photo tagged, even when another crop still has no assignment.
        photo_tags = (
            "SELECT pp.id pet_id,pp.name,pp.tag FROM reviews pr "
            "JOIN detections pd ON pd.id=pr.detection_id JOIN pets pp ON pp.id=pr.pet_id "
            "WHERE pd.asset_id=d.asset_id AND pd.revision=a.revision "
            f"AND pd.detector=d.detector AND pr.model=r.model AND {published} "
            "UNION SELECT pp.id,pp.name,pp.tag FROM tag_removals tr "
            "JOIN pets pp ON pp.tag=tr.tag WHERE tr.asset_id=d.asset_id"
        )
        if tagging == "untagged":
            clause += f" AND NOT EXISTS({photo_tags})"
        elif tagging == "tagged":
            # Completed assignments only: a photo's other animals must not leak
            # suggestions into Tagged -> a named pet.
            clause += " AND r.status='confirmed'"
            if not settings.demo:
                clause += " AND r.published=1"
        elif tagging == "unassigned":
            clause += " AND r.published=0"
            if settings.demo:
                clause += " AND r.status<>'confirmed'"
        if not include_dismissed:
            clause += (
                " AND (r.status<>'rejected' OR EXISTS(SELECT 1 FROM tag_removals tr "
                "WHERE tr.detection_id=d.id AND tr.model=r.model))"
            )
        if status != "all":
            clause += " AND r.status=?"
            params.append(status)
        if pet_id:
            clause += " AND r.pet_id=?"
            params.append(str(pet_id))
        if source != "all":
            clause += (
                f" AND {'NOT ' if source == 'manual' else ''}EXISTS("
                "SELECT 1 FROM automatic_reviews ar "
                "WHERE ar.detection_id=r.detection_id AND ar.model=r.model)"
            )
            clause += " AND r.status='confirmed'"
        counts = service.store.one(
            "SELECT count(*) total,count(DISTINCT a.id) photo_total " + clause, params
        )
        if unique_photos:
            counts["crop_total"] = counts["total"]
            counts["total"] = counts["photo_total"]
            clause += " GROUP BY a.id"
        related = (
            (
                "(SELECT json_group_array(dd.id) FROM detections dd JOIN reviews rr "
                "ON rr.detection_id=dd.id WHERE dd.asset_id=d.asset_id AND dd.revision=a.revision "
                "AND dd.detector=d.detector AND rr.model=r.model AND rr.pet_id IS r.pet_id "
                "AND rr.status=r.status) assignment_ids,"
                "(SELECT json_group_array(json_object('pet_id',names.id,'name',names.name)) FROM "
                "(SELECT DISTINCT pp.id,pp.name FROM detections dd JOIN reviews rr "
                "ON rr.detection_id=dd.id JOIN pets pp ON pp.id=rr.pet_id "
                "WHERE dd.asset_id=d.asset_id AND dd.revision=a.revision "
                "AND dd.detector=d.detector AND rr.model=r.model "
                "AND rr.status='confirmed') names) identities,"
            )
            if unique_photos
            else ""
        )
        rows = service.store.rows(
            "SELECT " + related + "d.id,d.asset_id,d.species,d.score detector_score,d.box,a.name,"
            "r.pet_id,r.score,r.margin,r.status,r.published,r.error,p.name pet_name,"
            "EXISTS(SELECT 1 FROM automatic_reviews ar WHERE ar.detection_id=r.detection_id "
            "AND ar.model=r.model) automatic,"
            "(SELECT json_group_array("
            "json_object('pet_id',pt.pet_id,'name',pt.name,'tag',pt.tag)) "
            f"FROM ({photo_tags}) pt) photo_tags "
            + clause
            + (
                " ORDER BY r.score DESC,d.rowid DESC"
                if sort == "suggested"
                else " ORDER BY d.rowid DESC"
            )
            + " LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        for row in rows:
            row["photo_tags"] = json.loads(row["photo_tags"])
            if unique_photos:
                row["assignment_ids"] = json.loads(row["assignment_ids"])
                row["identities"] = json.loads(row["identities"])
        return {
            "items": service.recommendations(rows),
            **counts,
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/detections/{detection_id}/photo", dependencies=auth)
    def photo(detection_id: str, size: int = Query(320, ge=64, le=1600)):
        row = service.store.one(
            "SELECT d.asset_id,d.revision,d.crop FROM detections d JOIN assets a "
            "ON a.id=d.asset_id WHERE d.id=? AND d.detector=? AND d.revision=a.revision",
            (detection_id, settings.detector_key),
        )
        if row is None:
            raise HTTPException(404, "Photo no longer matches this library entry")
        previews = settings.data_dir / "photo-previews"
        previews.mkdir(exist_ok=True)
        key = hashlib.sha256((row["asset_id"] + ":" + row["revision"]).encode()).hexdigest()
        target = previews / f"{key}-{size}.jpg"
        if not target.is_file():
            try:
                if settings.demo:
                    raw = (service.crops / row["crop"]).read_bytes()
                else:
                    asset = service.immich.asset(row["asset_id"])
                    if not eligible(asset) or asset_revision(asset) != row["revision"]:
                        raise HTTPException(404, "Photo changed or is unavailable")
                    raw = service.immich.preview(row["asset_id"])
                with Image.open(io.BytesIO(raw)) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((size, size))
                with tempfile.NamedTemporaryFile(dir=previews, suffix=".jpg", delete=False) as tmp:
                    temporary = Path(tmp.name)
                try:
                    image.save(temporary, "JPEG", quality=88 if size > 640 else 82, optimize=True)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            except HTTPException:
                raise
            except Exception as error:
                raise HTTPException(502, service.safe_error(error)) from None
        return FileResponse(
            target, headers={"Cache-Control": "private, max-age=86400", "Vary": "Authorization"}
        )

    @app.get("/api/detections/{detection_id}/crop", dependencies=auth)
    def crop(detection_id: str, size: int | None = Query(None, ge=64, le=1024)):
        row = service.store.one("SELECT crop FROM detections WHERE id=?", (detection_id,))
        if row is None:
            raise HTTPException(404, "Crop not found")
        path = service.crops / row["crop"]
        if not path.is_file():
            raise HTTPException(404, "Crop unavailable; reprocess this asset")
        if size:
            thumbnails = settings.data_dir / "thumbnails"
            thumbnails.mkdir(exist_ok=True)
            key = hashlib.sha256((detection_id + row["crop"]).encode()).hexdigest()
            thumbnail = thumbnails / f"{key}-{size}.jpg"
            if not thumbnail.is_file():
                with Image.open(path) as original:
                    image = original.convert("RGB")
                    image.thumbnail((size, size), Image.Resampling.LANCZOS)
                with tempfile.NamedTemporaryFile(
                    dir=thumbnails, suffix=".jpg", delete=False
                ) as tmp:
                    temporary = Path(tmp.name)
                try:
                    image.save(temporary, "JPEG", quality=82, optimize=True)
                    temporary.replace(thumbnail)
                finally:
                    temporary.unlink(missing_ok=True)
            return FileResponse(
                thumbnail,
                headers={"Cache-Control": "private, max-age=86400", "Vary": "Authorization"},
            )
        return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    @app.post("/api/detections/{detection_id}/decision", dependencies=auth)
    def decide(detection_id: str, body: Decision):
        if body.status == "confirmed" and body.pet_id is None:
            raise HTTPException(400, "Choose the pet to confirm")
        try:
            service.decide(
                detection_id,
                str(body.pet_id) if body.pet_id else None,
                body.status,
                body.status == "confirmed",
                str(body.expected_pet_id) if body.expected_pet_id else None,
                body.expected_status,
            )
            return {"status": body.status, "writeback": "demo only" if settings.demo else "queued"}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/review/batch", dependencies=auth)
    def bulk_review(body: BulkReview):
        return save_once(body, lambda: perform_bulk_review(body))

    def perform_bulk_review(body):
        results = service.bulk_decide(
            [
                {
                    "detection_id": item.detection_id,
                    "pet_id": str(item.pet_id) if item.pet_id else None,
                    "status": item.status,
                    "learn": item.status == "confirmed",
                    "expected_pet_id": str(item.expected_pet_id) if item.expected_pet_id else None,
                    "expected_status": item.expected_status,
                }
                for item in body.items
            ]
        )
        return {
            "results": results,
            "saved": sum(item["saved"] for item in results),
            "failed": sum(not item["saved"] for item in results),
            "writeback": "demo only" if settings.demo else "queued",
        }

    @app.post("/api/sync", dependencies=auth, status_code=202)
    def sync(full: bool = False):
        if full:
            service.backfill_requested.set()
        service.sync_requested.set()
        service.wake_event.set()
        return {"status": "queued", "mode": "backfill" if full else "incremental"}

    @app.post("/api/rematch", dependencies=auth, status_code=202)
    def rematch():
        service.gallery_changed()
        return {"status": "queued"}

    @app.post("/api/retry", dependencies=auth, status_code=202)
    def retry():
        service.retry_failed()
        return {"status": "queued"}

    @app.get("/api/jobs", dependencies=auth)
    def jobs():
        return service.store.rows(
            "SELECT j.id,a.name,j.status,j.attempts,j.error FROM jobs j "
            "JOIN assets a ON a.id=j.asset_id WHERE j.pipeline=? ORDER BY j.id DESC LIMIT 50",
            (settings.pipeline_key,),
        )

    return app
