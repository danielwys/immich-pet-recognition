import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .service import Service

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


class Decision(BaseModel):
    status: Literal["confirmed", "rejected"]
    pet_id: UUID | None = None


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

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if settings.demo:
            return
        token = credentials.credentials if credentials else ""
        if not secrets.compare_digest(token, settings.service_token.get_secret_value()):
            raise HTTPException(
                401, "Enter the service token", headers={"WWW-Authenticate": "Bearer"}
            )

    auth = [Depends(authenticate)]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/status", dependencies=auth)
    def status():
        return service.status()

    @app.get("/api/pets", dependencies=auth)
    def pets():
        return service.store.rows(
            "SELECT p.*,count(CASE WHEN r.kind='positive' THEN 1 END) positive_count,"
            "count(CASE WHEN r.kind='negative' THEN 1 END) negative_count FROM pets p "
            "LEFT JOIN reference_images r ON p.id=r.pet_id GROUP BY p.id ORDER BY p.name"
        )

    @app.post("/api/pets", dependencies=auth, status_code=201)
    def add_pet(body: PetCreate):
        try:
            return service.add_pet(body.name, body.species)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A pet with this name already exists") from None

    @app.post("/api/pets/{pet_id}/references", dependencies=auth, status_code=201)
    def add_reference(pet_id: UUID, body: ReferenceCreate):
        try:
            service.add_reference(str(pet_id), body.detection_id, body.kind)
            return {"status": "saved", "rematch": "queued"}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/api/detections", dependencies=auth)
    def detections(
        status: Literal["all", "pending", "confirmed", "rejected"] = "pending",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=100),
    ):
        clause = (
            "FROM detections d JOIN assets a ON a.id=d.asset_id "
            "JOIN reviews r ON r.detection_id=d.id LEFT JOIN pets p ON p.id=r.pet_id "
            "WHERE d.revision=a.revision AND d.detector=? AND r.model=?"
        )
        params = [settings.detector_key, settings.recognizer_key]
        if status != "all":
            clause += " AND r.status=?"
            params.append(status)
        count = service.store.one("SELECT count(*) total " + clause, params)["total"]
        rows = service.store.rows(
            "SELECT d.id,d.asset_id,d.species,d.score detector_score,d.box,a.name,"
            "r.pet_id,r.score,r.margin,r.status,r.published,r.error,p.name pet_name "
            + clause
            + " ORDER BY d.rowid DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return {"items": rows, "total": count, "offset": offset, "limit": limit}

    @app.get("/api/detections/{detection_id}/crop", dependencies=auth)
    def crop(detection_id: str):
        row = service.store.one("SELECT crop FROM detections WHERE id=?", (detection_id,))
        if row is None:
            raise HTTPException(404, "Crop not found")
        path = service.crops / row["crop"]
        if not path.is_file():
            raise HTTPException(404, "Crop unavailable; reprocess this asset")
        return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    @app.post("/api/detections/{detection_id}/decision", dependencies=auth)
    def decide(detection_id: str, body: Decision):
        if body.status == "confirmed" and body.pet_id is None:
            raise HTTPException(400, "Choose the pet to confirm")
        try:
            service.decide(detection_id, str(body.pet_id) if body.pet_id else None, body.status)
            return {"status": body.status, "writeback": "demo only" if settings.demo else "queued"}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/sync", dependencies=auth, status_code=202)
    def sync(full: bool = False):
        if full:
            service.backfill_requested.set()
        service.sync_requested.set()
        service.wake_event.set()
        return {"status": "queued", "mode": "backfill" if full else "incremental"}

    @app.post("/api/rematch", dependencies=auth, status_code=202)
    def rematch():
        service.rematch_requested.set()
        service.wake_event.set()
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
