import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
 id TEXT PRIMARY KEY, revision TEXT NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, asset_id TEXT NOT NULL, revision TEXT NOT NULL,
 pipeline TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0, error TEXT,
 UNIQUE(asset_id, revision, pipeline)
);
CREATE TABLE IF NOT EXISTS passes (
 asset_id TEXT NOT NULL, revision TEXT NOT NULL, detector TEXT NOT NULL,
 PRIMARY KEY(asset_id, revision, detector)
);
CREATE TABLE IF NOT EXISTS detections (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, revision TEXT NOT NULL, detector TEXT NOT NULL,
 species TEXT NOT NULL, score REAL NOT NULL, box TEXT NOT NULL, crop TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
 detection_id TEXT NOT NULL REFERENCES detections(id), model TEXT NOT NULL, vector BLOB NOT NULL,
 PRIMARY KEY(detection_id, model)
);
CREATE TABLE IF NOT EXISTS pets (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 species TEXT NOT NULL, tag TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS reference_images (
 pet_id TEXT NOT NULL REFERENCES pets(id), detection_id TEXT NOT NULL REFERENCES detections(id),
 kind TEXT NOT NULL CHECK(kind IN ('positive', 'negative')),
 PRIMARY KEY(pet_id, detection_id)
);
CREATE TABLE IF NOT EXISTS reviews (
 detection_id TEXT NOT NULL REFERENCES detections(id), model TEXT NOT NULL,
 pet_id TEXT REFERENCES pets(id), score REAL NOT NULL, margin REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', published INTEGER NOT NULL DEFAULT 0,
 error TEXT, PRIMARY KEY(detection_id, model)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pet_automation (
 pet_id TEXT PRIMARY KEY REFERENCES pets(id), enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS automatic_reviews (
 detection_id TEXT NOT NULL, model TEXT NOT NULL,
 PRIMARY KEY(detection_id, model),
 FOREIGN KEY(detection_id, model) REFERENCES reviews(detection_id, model)
);
CREATE TABLE IF NOT EXISTS tag_removals (
 detection_id TEXT NOT NULL, model TEXT NOT NULL, asset_id TEXT NOT NULL, tag TEXT NOT NULL,
 PRIMARY KEY(detection_id, model, tag),
 FOREIGN KEY(detection_id, model) REFERENCES reviews(detection_id, model)
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at);
CREATE INDEX IF NOT EXISTS detections_asset ON detections(asset_id, revision, detector);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def rows(self, query, params=()):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def one(self, query, params=()):
        rows = self.rows(query, params)
        return rows[0] if rows else None

    def execute(self, query, params=()):
        with self.connection() as conn:
            return conn.execute(query, params).rowcount

    def state(self, key, default=None):
        row = self.one("SELECT value FROM state WHERE key=?", (key,))
        return row["value"] if row else default

    def set_state(self, key, value):
        self.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))

    def enqueue(self, asset, revision, pipeline):
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
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO assets VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision,name=excluded.name",
                (asset["id"], revision, asset.get("originalFileName", asset["id"])),
            )
            result = conn.execute(
                "INSERT OR IGNORE INTO jobs(asset_id,revision,pipeline,payload) VALUES (?,?,?,?)",
                (asset["id"], revision, pipeline, json.dumps(metadata)),
            )
            return result.rowcount

    def claim(self, pipeline):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND available_at<=? AND pipeline=? "
                "ORDER BY id LIMIT 1",
                (time.time(), pipeline),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status='processing',attempts=attempts+1,error=NULL WHERE id=?",
                (row["id"],),
            )
            job = dict(row)
            job["attempts"] += 1
            return job

    def embedding(self, detection_id, model):
        row = self.one(
            "SELECT vector FROM embeddings WHERE detection_id=? AND model=?", (detection_id, model)
        )
        return np.frombuffer(row["vector"], dtype=np.float32).copy() if row else None

    def save_embedding(self, detection_id, model, vector):
        self.execute(
            "INSERT OR REPLACE INTO embeddings VALUES (?,?,?)",
            (detection_id, model, np.asarray(vector, dtype=np.float32).tobytes()),
        )
