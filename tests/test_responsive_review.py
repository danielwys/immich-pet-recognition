import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image
from test_groups import photos

from immich_pet_recognition.api import create_app
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def split_groups(service):
    rows = photos(service, 6)
    for index, row in enumerate(rows):
        vector = np.eye(3, dtype=np.float32)[index // 2]
        service.store.save_embedding(row["id"], service.settings.recognizer_key, vector)
    return service.groups()


def test_many_selected_groups_are_one_transaction_one_learning_pass_and_one_undo(service):
    groups = split_groups(service)
    pet = service.add_pet("Rusty", "dog")
    before = int(service.store.state("gallery_revision", "0"))
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        result = client.post(
            "/api/groups/batch",
            json={"group_ids": [group["id"] for group in groups], "pet_id": pet["id"]},
        )
        assert result.status_code == 200
        assert result.json()["saved"] == 6 and result.json()["skipped"] == 0
        assert int(service.store.state("gallery_revision")) == before + 1
        assert service.store.one("SELECT count(*) n FROM reference_images")["n"] == 6
        assert client.post("/api/groups/undo/" + result.json()["undo_id"]).json()["restored"] == 6
        assert service.store.one("SELECT count(*) n FROM reviews WHERE status='pending'")["n"] == 6


def test_queue_and_next_decision_stay_available_during_background_policy_fit(service, monkeypatch):
    groups = split_groups(service)
    pet = service.add_pet("Rusty", "dog")
    service.settings.matching_mode = "adaptive"
    entered, release = Event(), Event()

    def slow_fit(gallery, *args):
        entered.set()
        assert release.wait(5)
        return {
            row["id"]: {"threshold": 0.85, "margin": 0.08, "min_references": 10} for row in gallery
        }

    monkeypatch.setattr("immich_pet_recognition.service.fit_policies", slow_fit)
    first = service.decide_group(groups[0]["id"], pet["id"], [])
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(service.rematch)
            try:
                assert entered.wait(3)
                page = pool.submit(lambda: client.get("/api/groups").json()).result(1)
                assert page["updating"] is True and page["total_groups"] == 2
                for route in ("status", "pets", "collections", "matching"):
                    assert (
                        pool.submit(lambda path=route: client.get("/api/" + path))
                        .result(1)
                        .status_code
                        == 200
                    )
                second = pool.submit(
                    lambda: client.post(
                        "/api/groups/batch",
                        json={"group_ids": [groups[1]["id"]], "pet_id": pet["id"]},
                    )
                ).result(1)
                assert second.status_code == 200 and second.json()["saved"] == 2
                assert second.json()["undo_id"] != first["undo_id"]
            finally:
                release.set()
            worker.result(3)
        assert service.suggestions_pending()  # The fit cannot claim the newer human decision.
        service.rematch()
        assert not service.suggestions_pending()


def test_invalid_batch_does_not_partially_save_and_changed_members_are_skipped(service):
    groups = split_groups(service)
    pet = service.add_pet("Rusty", "dog")
    other = service.add_pet("Butter", "dog")
    changed = groups[0]["members"][0]["id"]
    service.decide(changed, other["id"], "confirmed")
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        result = client.post(
            "/api/groups/batch",
            json={"group_ids": [groups[0]["id"], "expired"], "pet_id": pet["id"]},
        )
        assert result.status_code == 409
        assert (
            service.store.one("SELECT count(*) n FROM reviews WHERE status='confirmed'")["n"] == 1
        )
        result = client.post(
            "/api/groups/batch",
            json={"group_ids": [group["id"] for group in groups], "pet_id": pet["id"]},
        )
        assert result.status_code == 200
        assert result.json()["saved"] == 5 and result.json()["skipped"] == 1
        assert (
            service.store.one("SELECT pet_id FROM reviews WHERE detection_id=?", (changed,))[
                "pet_id"
            ]
            == other["id"]
        )


def test_slow_immich_publication_does_not_hold_up_a_new_identity_or_lose_compensation(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    butter = processed.add_pet("Butter", "dog")
    rusty = processed.add_pet("Rusty", "dog")
    processed.decide(detection, butter["id"], "confirmed")
    entered, release = Event(), Event()
    tag = processed.immich.tag

    def slow_tag(*args):
        entered.set()
        assert release.wait(5)
        tag(*args)

    processed.immich.tag = slow_tag
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(processed.publish_one)
        try:
            assert entered.wait(3)
            pool.submit(processed.decide, detection, rusty["id"], "confirmed").result(1)
        finally:
            release.set()
        assert publication.result(3)
    review = processed.store.one("SELECT * FROM reviews")
    assert review["pet_id"] == rusty["id"] and review["published"] == 0
    assert processed.store.one("SELECT count(*) n FROM tag_removals")["n"] == 1
    processed.immich.tag = tag
    restarted = Service(
        processed.settings, processed.immich, processed.detector, processed.recognizer
    )
    while restarted.publish_one():
        pass
    assert processed.immich.assigned_tags[processed.immich.items[0]["id"]] == {"Pets/Rusty"}
    assert restarted.store.one("SELECT count(*) n FROM tag_removals")["n"] == 0


def test_slow_removal_cannot_mark_a_new_assignment_unpublished_or_remove_its_debt(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Rusty", "dog")
    processed.decide(detection, pet["id"], "confirmed")
    processed.publish_one()
    processed.decide(detection, None, "rejected")
    entered, release = Event(), Event()
    untag = processed.immich.untag

    def slow_untag(*args):
        entered.set()
        assert release.wait(5)
        untag(*args)

    processed.immich.untag = slow_untag
    with ThreadPoolExecutor(max_workers=2) as pool:
        removal = pool.submit(processed.publish_one)
        try:
            assert entered.wait(3)
            pool.submit(processed.decide, detection, pet["id"], "confirmed").result(1)
        finally:
            release.set()
        assert removal.result(3)
    processed.immich.untag = untag
    assert processed.store.one("SELECT published FROM reviews")["published"] == 0
    while processed.publish_one():
        pass
    assert processed.immich.assigned_tags[processed.immich.items[0]["id"]] == {"Pets/Rusty"}


def test_thumbnails_are_small_authenticated_immutable_and_cached(processed):
    detection = processed.store.one("SELECT id,crop FROM detections")
    Image.new("RGB", (1600, 1000), "red").save(processed.crops / detection["crop"], "JPEG")
    app = create_app(processed.settings, processed)
    with TestClient(app) as client:
        assert client.get(f"/api/detections/{detection['id']}/crop?size=320").status_code == 401
        response = client.get(f"/api/detections/{detection['id']}/crop?size=320", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, max-age=86400"
        with Image.open(io.BytesIO(response.content)) as thumbnail:
            assert thumbnail.size == (320, 200)
        files = list((processed.settings.data_dir / "thumbnails").iterdir())
        assert len(files) == 1
        before = files[0].stat().st_mtime_ns
        again = client.get(f"/api/detections/{detection['id']}/crop?size=320", headers=AUTH)
        assert again.content == response.content and files[0].stat().st_mtime_ns == before
        assert (
            client.get(
                f"/api/detections/{detection['id']}/crop?size=1025", headers=AUTH
            ).status_code
            == 422
        )
        assert (
            client.get(f"/api/detections/{detection['id']}/crop", headers=AUTH).headers[
                "cache-control"
            ]
            == "private, no-store"
        )


def test_saving_a_served_selection_never_rebuilds_the_library(service, monkeypatch):
    groups = split_groups(service)
    pet = service.add_pet("Rusty", "dog")
    service.group_cache = None

    def unavailable():
        raise AssertionError("A human save must not rebuild or read all groups")

    monkeypatch.setattr(service, "groups", unavailable)
    result = service.decide_groups([groups[0]["id"]], pet["id"])
    assert result["saved"] == 2


def test_large_group_read_does_not_hold_the_human_decision_lock(service, monkeypatch):
    rows = photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    entered, release = Event(), Event()
    original = service.store.rows

    def slow_rows(query, params=()):
        if query.startswith("WITH tagged AS MATERIALIZED"):
            entered.set()
            assert release.wait(5)
        return original(query, params)

    monkeypatch.setattr(service.store, "rows", slow_rows)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(service.groups)
        try:
            assert entered.wait(3)
            pool.submit(service.decide, rows[0]["id"], pet["id"], "confirmed").result(1)
        finally:
            release.set()
        assert sum(group["photo_count"] for group in read.result(3)) == 1
    assert sum(group["photo_count"] for group in service.groups()) == 1
