from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_groups import photos

from immich_pet_recognition.api import create_app
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def test_edit_empty_pet_keeps_identity_and_automation_after_restart(service):
    pet = service.add_pet("Mimi", "dog")
    service.set_automation(pet["id"], False)
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        response = client.put(f"/api/pets/{pet['id']}", json={"name": "Mimi", "species": "cat"})
        assert response.status_code == 200
        assert response.json() == {**pet, "species": "cat"}
    restarted = Service(service.settings, service.immich, service.detector, service.recognizer)
    saved = restarted.pets()[0]
    assert (saved["id"], saved["name"], saved["species"], saved["auto_tag"]) == (
        pet["id"],
        "Mimi",
        "cat",
        False,
    )
    assert restarted.store.one("SELECT count(*) n FROM pets")["n"] == 1


def test_rename_and_species_edit_preserve_album_cover_feedback_and_immich_names(processed):
    service = processed
    detection = service.store.one("SELECT id FROM detections")["id"]
    pet = service.add_pet("Mimi", "dog")
    service.decide(detection, pet["id"], "confirmed")
    service.store.set_state("pet_cover:" + pet["id"], detection)
    assert service.publish_one()
    references = service.store.rows("SELECT * FROM reference_images")
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        response = client.put(f"/api/pets/{pet['id']}", json={"name": "Mimi Cat", "species": "cat"})
        assert response.status_code == 200
        assert response.json()["tag"] == "Pets/Mimi Cat"
        album = client.get("/api/collections").json()[0]
        assert album["id"] == pet["id"]
        assert album["cover_id"] == detection
        assert album["photo_count"] == 1
        assert service.store.rows("SELECT * FROM reference_images") == references
        assert service.store.one("SELECT species FROM detections")["species"] == "cat"
        while service.publish_one():
            pass
        assert service.immich.assigned_tags[service.immich.items[0]["id"]] == {"Pets/Mimi Cat"}
        assert service.store.rows("SELECT * FROM tag_removals") == []
        assert service.store.one("SELECT pet_id,status,published FROM reviews") == {
            "pet_id": pet["id"],
            "status": "confirmed",
            "published": 1,
        }


def test_invalid_duplicate_missing_and_unauthorized_edit_leave_pet_unchanged(service):
    pet = service.add_pet("Mimi", "dog")
    service.add_pet("Butter", "dog")
    endpoint = f"/api/pets/{pet['id']}"
    with TestClient(create_app(service.settings, service)) as client:
        assert client.put(endpoint, json={"name": "Mimi", "species": "cat"}).status_code == 401
        for body in [{"name": " ", "species": "cat"}, {"name": "Mimi", "species": "bird"}]:
            assert client.put(endpoint, json=body, headers=AUTH).status_code == 422
        assert (
            client.put(
                endpoint, json={"name": "butter", "species": "cat"}, headers=AUTH
            ).status_code
            == 409
        )
        assert (
            client.put(
                f"/api/pets/{uuid4()}", json={"name": "Mimi", "species": "cat"}, headers=AUTH
            ).status_code
            == 404
        )
    assert service.store.one("SELECT * FROM pets WHERE id=?", (pet["id"],)) == pet
    assert service.store.rows("SELECT * FROM tag_removals") == []


def test_noop_edit_does_not_queue_recognition_or_tag_changes(service):
    pet = service.add_pet("Mimi", "cat")
    revision = service.store.state("gallery_revision")
    assert service.update_pet(pet["id"], "Mimi", "cat") == pet
    assert service.store.state("gallery_revision") == revision
    assert service.store.rows("SELECT * FROM tag_removals") == []


def test_species_edit_updates_existing_photos_and_removes_incompatible_feedback(service):
    rows = photos(service, 3)
    pet = service.add_pet("Mimi", "dog")
    other = service.add_pet("Rusty", "dog")
    service.decide(rows[0]["id"], pet["id"], "confirmed")
    service.add_reference(other["id"], rows[0]["id"], "negative")
    service.add_reference(pet["id"], rows[1]["id"], "negative")
    service.decide(rows[2]["id"], other["id"], "confirmed")
    service.update_pet(pet["id"], "Mimi", "cat")
    assert (
        service.store.one("SELECT species FROM detections WHERE id=?", (rows[0]["id"],))["species"]
        == "cat"
    )
    assert (
        service.store.one("SELECT species FROM detections WHERE id=?", (rows[2]["id"],))["species"]
        == "dog"
    )
    assert (
        service.store.rows(
            "SELECT pet_id,detection_id,kind FROM reference_images WHERE kind='negative'"
        )
        == []
    )
    assert len(service.references()[0]["positive"]) == 1
    assert service.rematch_requested.is_set()


def test_species_edit_invalidates_only_an_undo_involving_this_pet(service):
    photos(service, 2)
    pet = service.add_pet("Mimi", "dog")
    other = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    service.decide_group(group["id"], other["id"], [])
    original_undo = service.undo_info()
    service.update_pet(pet["id"], "Mimi", "cat")
    assert service.undo_info() == original_undo
    service.update_pet(other["id"], "Rusty", "cat")
    assert service.undo_info() is None
    assert all(
        row["species"] == "cat" for row in service.store.rows("SELECT species FROM detections")
    )


def test_rename_during_tag_http_write_does_not_lose_new_name(processed):
    service = processed
    pet = service.add_pet("Mimi", "dog")
    detection = service.store.one("SELECT id FROM detections")["id"]
    service.decide(detection, pet["id"], "confirmed")
    entered, release = Event(), Event()
    original = service.immich.tag

    def held_tag(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    service.immich.tag = held_tag
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(service.publish_one)
        try:
            assert entered.wait(3)
            pool.submit(service.update_pet, pet["id"], "Mimi Cat", "dog").result(1)
        finally:
            release.set()
        publication.result(3)
    assert service.store.one("SELECT published FROM reviews")["published"] == 0
    service.immich.tag = original
    while service.publish_one():
        pass
    assert service.immich.assigned_tags[service.immich.items[0]["id"]] == {"Pets/Mimi Cat"}
    assert service.store.rows("SELECT * FROM tag_removals") == []


@pytest.mark.parametrize("failure", ["fail_tag", "fail_untag"])
def test_rename_tag_sync_failure_is_retryable_without_losing_identity(processed, failure):
    service = processed
    pet = service.add_pet("Mimi", "dog")
    detection = service.store.one("SELECT id FROM detections")["id"]
    service.decide(detection, pet["id"], "confirmed")
    service.publish_one()
    service.update_pet(pet["id"], "Mimi Cat", "dog")
    setattr(service.immich, failure, True)
    service.publish_one()
    row = service.store.one("SELECT * FROM reviews")
    assert row["pet_id"] == pet["id"] and row["status"] == "confirmed" and row["error"]
    assert service.store.rows("SELECT * FROM tag_removals")
    setattr(service.immich, failure, False)
    service.retry_failed()
    while service.publish_one():
        pass
    assert service.immich.assigned_tags[service.immich.items[0]["id"]] == {"Pets/Mimi Cat"}
