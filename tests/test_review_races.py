from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from uuid import uuid4

import pytest
from test_groups import photos


def test_old_gallery_snapshot_cannot_be_cached_as_newer_human_revision(service, monkeypatch):
    rows = photos(service, 3)
    pet = service.add_pet("Rusty", "dog")
    service.settings.matching_mode = "adaptive"
    service.decide(rows[0]["id"], pet["id"], "confirmed")
    original = service.policies
    changed = False

    def fit(gallery, *args):
        return {
            p["id"]: {
                "threshold": 1.01,
                "margin": 0.02,
                "min_references": 3,
                "positive_checked": len(p["positive"]),
            }
            for p in gallery
        }

    def policies(gallery=None, **kwargs):
        nonlocal changed
        if gallery is not None and kwargs.get("refresh") and not changed:
            changed = True
            service.decide(rows[1]["id"], pet["id"], "confirmed")
        return original(gallery, **kwargs)

    monkeypatch.setattr("immich_pet_recognition.service.fit_policies", fit)
    monkeypatch.setattr(service, "policies", policies)
    service.rematch()
    revision = service.store.state("gallery_revision")
    assert not service.policy_cache or service.policy_cache[0][0] != revision
    monkeypatch.setattr(service, "policies", original)
    service.rematch()
    assert service.policy_cache[0][0] == revision
    assert service.policy_cache[1][pet["id"]]["positive_checked"] == 2


def test_pausing_during_external_auto_tag_write_compensates_without_orphan_tag(processed):
    service = processed
    detection = service.store.one("SELECT id FROM detections")["id"]
    pet = service.add_pet("Rusty", "dog")
    service.set_automation(pet["id"], True)
    service.decide(detection, pet["id"], "confirmed")
    service.store.execute(
        "INSERT INTO automatic_reviews VALUES (?,?)", (detection, service.settings.recognizer_key)
    )
    entered, release = Event(), Event()
    original = service.immich.tag

    def tag(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    service.immich.tag = tag
    with ThreadPoolExecutor(max_workers=2) as pool:
        publish = pool.submit(service.publish_one)
        try:
            assert entered.wait(3)
            pool.submit(service.set_automation, pet["id"], False).result(1)
        finally:
            release.set()
        publish.result(3)
    service.immich.tag = original
    while service.publish_one():
        pass
    assert not service.immich.assigned_tags[service.immich.items[0]["id"]]
    assert service.store.one("SELECT count(*) n FROM tag_removals")["n"] == 0
    assert service.store.one("SELECT status FROM reviews")["status"] == "pending"


def test_group_cache_does_not_capture_uncommitted_state_under_new_generation(service):
    row = photos(service, 1)[0]
    entered, release = Event(), Event()

    def change():
        with service.model_lock:
            with service.store.connection() as conn:
                conn.execute(
                    "UPDATE reviews SET status='rejected' WHERE detection_id=?", (row["id"],)
                )
                service._review_changed(conn)
                entered.set()
                assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(change)
        try:
            assert entered.wait(3)
            reading = pool.submit(service.groups)
            with pytest.raises(TimeoutError):
                reading.result(0.05)
        finally:
            release.set()
        mutation.result(3)
        assert reading.result(3) == []
    assert service.groups() == []


def test_new_photo_http_download_does_not_block_existing_photo_naming(processed):
    service = processed
    existing = service.store.one("SELECT id FROM detections")["id"]
    pet = service.add_pet("Rusty", "dog")
    new_id = str(uuid4())
    service.immich.items.append({**service.immich.items[0], "id": new_id})
    service.sync()
    entered, release = Event(), Event()
    original = service.immich.asset

    def asset(asset_id):
        if asset_id == new_id:
            entered.set()
            assert release.wait(5)
        return original(asset_id)

    service.immich.asset = asset
    with ThreadPoolExecutor(max_workers=2) as pool:
        scan = pool.submit(service.process_one)
        try:
            assert entered.wait(3)
            pool.submit(service.decide, existing, pet["id"], "confirmed").result(1)
        finally:
            release.set()
        assert scan.result(3)
    assert (
        service.store.one("SELECT pet_id FROM reviews WHERE detection_id=?", (existing,))["pet_id"]
        == pet["id"]
    )


def test_retry_after_lost_ack_is_idempotent_and_survives_restart(service):
    from fastapi.testclient import TestClient

    from immich_pet_recognition.api import create_app
    from immich_pet_recognition.service import Service

    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    auth = {"Authorization": "Bearer test-token"}
    body = {"operation_id": "lost-ack", "group_ids": [group["id"]], "pet_id": pet["id"]}
    with TestClient(create_app(service.settings, service), headers=auth) as client:
        first = client.post("/api/groups/batch", json=body)
        assert first.status_code == 200
        revision = service.store.state("gallery_revision")
        assert client.post("/api/groups/batch", json=body).json() == first.json()
        assert service.store.state("gallery_revision") == revision
    restarted = Service(service.settings, service.immich, service.detector, service.recognizer)
    with TestClient(create_app(service.settings, restarted), headers=auth) as client:
        assert client.post("/api/groups/batch", json=body).json() == first.json()
        assert client.post("/api/groups/batch", json={**body, "skip": True}).status_code == 409
        undo_path = "/api/groups/undo/" + first.json()["undo_id"]
        undone = client.post(undo_path, json={"operation_id": "undo-lost-ack"})
        assert undone.status_code == 200
        assert (
            client.post(undo_path, json={"operation_id": "undo-lost-ack"}).json() == undone.json()
        )
        assert (
            restarted.store.one("SELECT count(*) n FROM reviews WHERE status='pending'")["n"] == 2
        )


def test_removing_a_name_returns_photo_to_unidentified_and_does_not_relearn_old_pet(processed):
    from fastapi.testclient import TestClient

    from immich_pet_recognition.api import create_app

    service = processed
    detection = service.store.one("SELECT id FROM detections")["id"]
    pet = service.add_pet("Rusty", "dog")
    service.decide(detection, pet["id"], "confirmed")
    service.publish_one()
    with TestClient(
        create_app(service.settings, service), headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post(
            "/api/review/batch",
            json={
                "items": [
                    {
                        "detection_id": detection,
                        "status": "pending",
                        "expected_pet_id": pet["id"],
                        "expected_status": "confirmed",
                    }
                ]
            },
        )
        assert response.json()["saved"] == 1
        while service.publish_one():
            pass
        assert client.get("/api/groups").json()["total_photos"] == 1
        assert service.store.one("SELECT kind FROM reference_images")["kind"] == "negative"
        assert not service.immich.assigned_tags[service.immich.items[0]["id"]]
