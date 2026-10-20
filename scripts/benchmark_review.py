"""Measure review responsiveness using an isolated synthetic photo library.

Run with ``uv run python scripts/benchmark_review.py``. No network, models, real
photos, or production database are used. Results include policy preparation,
cached reads, and consecutive durable decisions while rechecking is active.
"""

import argparse
import inspect
import json
import os
import threading
import time
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

# Small gallery operations should not create a large BLAS worker pool.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from immich_pet_recognition.api import create_app  # noqa: E402
from immich_pet_recognition.config import Settings  # noqa: E402
from immich_pet_recognition.service import Service  # noqa: E402


class NoExternalAccess:
    def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError(f"Benchmark must not call external component: {name}")


def identity(value):
    return str(uuid5(NAMESPACE_URL, "immich-review-benchmark/" + str(value)))


def unit(vector):
    return np.asarray(vector / np.linalg.norm(vector), dtype=np.float32)


def seed(service, references, pending, dimensions):
    rng = np.random.default_rng(20261020)
    centers = [unit(rng.normal(size=dimensions)) for _ in range(3)]
    pets = [identity(f"pet-{i}") for i in range(3)]
    noise = 1 / np.sqrt(dimensions)
    with service.store.connection() as conn:
        conn.executemany(
            "INSERT INTO pets VALUES (?,?,'dog',?)",
            [
                (pet, f"Synthetic pet {i + 1}", f"Pets/Synthetic{i + 1}")
                for i, pet in enumerate(pets)
            ],
        )
        conn.executemany("INSERT INTO pet_automation VALUES (?,0)", [(pet,) for pet in pets])
        previous = None
        for index in range(references + pending):
            labelled = index < references
            pet_index = index % 3
            if labelled:
                vector = unit(centers[pet_index] + rng.normal(scale=noise * 0.45, size=dimensions))
            elif index % 15 in (1, 2, 3) and previous is not None:
                vector, pet_index = previous
                vector = unit(vector + rng.normal(scale=noise * 0.03, size=dimensions))
            else:
                vector = unit(centers[pet_index] * 0.65 + rng.normal(scale=noise, size=dimensions))
            previous = (vector, pet_index)
            asset, detection, pet = identity(f"asset-{index}"), identity(index), pets[pet_index]
            conn.execute("INSERT INTO assets VALUES (?,'v1',?)", (asset, f"synthetic-{index}.jpg"))
            conn.execute(
                "INSERT INTO detections VALUES (?,?,'v1',?,'dog',0.95,'[0,0,100,100]',?)",
                (detection, asset, service.settings.detector_key, detection + ".jpg"),
            )
            conn.execute(
                "INSERT INTO embeddings VALUES (?,?,?)",
                (detection, service.settings.recognizer_key, vector.tobytes()),
            )
            conn.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    detection,
                    service.settings.recognizer_key,
                    pet,
                    0.9 if labelled else 0.6,
                    0.2 if labelled else 0.05,
                    "confirmed" if labelled else "pending",
                    int(labelled),
                ),
            )
            if labelled:
                conn.execute(
                    "INSERT INTO reference_images VALUES (?,?,'positive')", (pet, detection)
                )
        conn.execute("INSERT OR REPLACE INTO state VALUES ('gallery_revision','0')")
        conn.execute("INSERT OR REPLACE INTO state VALUES ('matched_gallery_revision','0')")
    return pets


def timed_request(client, method, path, **kwargs):
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    return response, round((time.perf_counter() - started) * 1000, 1)


def run(service, decisions, groups_per_decision):
    result = {}
    with TestClient(create_app(service.settings, service)) as client:
        # TestClient's lifespan starts no worker because background=False.
        started = time.perf_counter()
        options = (
            {"refresh": True} if "refresh" in inspect.signature(service.policies).parameters else {}
        )
        service.policies(service.references(), **options)
        result["policy_prepare_ms"] = round((time.perf_counter() - started) * 1000, 1)
        response, elapsed = timed_request(client, "GET", "/api/groups?limit=48")
        response.raise_for_status()
        groups = response.json()
        result["initial_groups_ms"] = elapsed
        result["group_count"] = groups["total_groups"]
        result["photo_count"] = groups["total_photos"]
        pet_id = groups["items"][0]["pet_id"]
        response, elapsed = timed_request(client, "GET", f"/api/groups?pet_id={pet_id}&limit=48")
        response.raise_for_status()
        result["cached_groups_ms"] = elapsed
        snapshots = response.json()["items"]
        batches = [
            snapshots[index : index + groups_per_decision]
            for index in range(0, decisions * groups_per_decision, groups_per_decision)
        ]
        assert len(batches[-1]) == groups_per_decision, (
            "Seed more pending photos for this benchmark"
        )
        stop = threading.Event()
        worker_errors = []

        def recheck_loop():
            try:
                while not stop.is_set():
                    if service.rematch_requested.wait(0.02):
                        service.rematch_requested.clear()
                        service.rematch()
            except Exception as error:
                worker_errors.append(str(error))

        worker = threading.Thread(target=recheck_loop, daemon=True)
        worker.start()
        writes = []
        try:
            for batch in batches:
                body = {"group_ids": [row["id"] for row in batch], "pet_id": pet_id, "skip": False}
                response, elapsed = timed_request(client, "POST", "/api/groups/batch", json=body)
                contract = "batch"
                legacy_saved = 0
                if response.status_code in (404, 405):
                    # Baseline releases support only one group per request.
                    contract = "legacy_single_group"
                    body.pop("group_ids")
                    started = time.perf_counter()
                    for group in batch:
                        response = client.post(f"/api/groups/{group['id']}/decision", json=body)
                        if response.status_code != 200:
                            break
                        legacy_saved += response.json()["saved"]
                    elapsed = round((time.perf_counter() - started) * 1000, 1)
                writes.append(
                    {
                        "duration_ms": elapsed,
                        "contract": contract,
                        "status": response.status_code,
                        "body": response.json(),
                        "saved_before_legacy_failure": legacy_saved,
                        "recheck": service.recheck_status()["state"],
                    }
                )
                response, elapsed = timed_request(client, "GET", "/api/groups?limit=12")
                response.raise_for_status()
                payload = response.json()
                result.setdefault("reads_during_recheck", []).append(
                    {
                        "duration_ms": elapsed,
                        "visible_groups": len(payload["items"]),
                        "updating": payload["updating"],
                    }
                )
            result["decisions"] = writes
            result["all_decisions_saved"] = all(row["status"] == 200 for row in writes)
            result["groups_stayed_available"] = all(
                row["visible_groups"] for row in result["reads_during_recheck"]
            )
        finally:
            stop.set()
            service.stop_event.set()
            worker.join(timeout=30)
            assert not worker.is_alive(), "Recheck did not stop promptly"
        result["worker_errors"] = worker_errors
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=int, default=300)
    parser.add_argument("--pending", type=int, default=2500)
    parser.add_argument("--dimensions", type=int, default=512)
    parser.add_argument("--decisions", type=int, default=5)
    parser.add_argument("--groups-per-decision", type=int, default=3)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if a decision fails or rechecking empties the board",
    )
    args = parser.parse_args()
    with TemporaryDirectory(prefix="immich-review-benchmark-") as directory:
        settings = Settings(
            _env_file=None,
            data_dir=directory,
            auth_required=False,
            background=False,
            matching_mode="adaptive",
        )
        service = Service(settings, NoExternalAccess(), NoExternalAccess(), NoExternalAccess())
        seed(service, args.references, args.pending, args.dimensions)
        result = run(service, args.decisions, args.groups_per_decision)
        print(json.dumps({"library": vars(args), **result}, indent=2))
        if args.check:
            assert result["all_decisions_saved"], "Consecutive decisions must remain writable"
            assert result["groups_stayed_available"], "Rechecking must not empty the review board"
            assert not result["worker_errors"], result["worker_errors"]


if __name__ == "__main__":
    main()
