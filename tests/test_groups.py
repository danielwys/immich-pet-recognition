from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient

from immich_pet_recognition.api import create_app
from immich_pet_recognition.grouping import similar_groups
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def photos(service, count=3):
    original = service.immich.items[0]
    service.immich.items.extend({**original, "id": str(uuid4())} for _ in range(count - 1))
    service.sync()
    while service.process_one():
        pass
    return service.store.rows("SELECT id FROM detections")


def test_groups_have_a_pairwise_bound_and_never_chain_distant_photos():
    rows = [
        {
            "id": str(i),
            "asset_id": str(i),
            "species": "dog",
            "pet_id": None,
            "vector": np.array([np.cos(a), np.sin(a)]),
        }
        for i, a in enumerate(np.deg2rad([0, 20, 40]))
    ]
    groups = similar_groups(rows, similarity=0.9)
    assert sorted(group["crop_count"] for group in groups) == [1, 2]
    assert {row["id"] for group in groups for row in group["members"]} == {"0", "1", "2"}


def test_groups_are_bounded_and_separate_pet_suggestions_and_species():
    rows = [
        {
            "id": str(i),
            "asset_id": str(i // 2),
            "species": "dog",
            "pet_id": "Rusty",
            "vector": np.array([1, 0]),
        }
        for i in range(401)
    ]
    rows += [
        {**rows[0], "id": "cat", "species": "cat"},
        {**rows[0], "id": "Wang", "pet_id": "Wang"},
    ]
    groups = similar_groups(rows)
    assert sorted(group["crop_count"] for group in groups) == [1, 1, 1, 200, 200]
    assert all("vector" not in member for group in groups for member in group["members"])


def test_group_assignment_exclusions_teach_once_and_undo_includes_propagated_auto_matches(service):
    photos(service)
    pet = service.add_pet("Rusty", "dog")
    service.settings.min_references = 1
    service.settings.auto_tag = True
    group = service.groups()[0]
    excluded = group["members"][-1]["id"]
    revision = int(service.store.state("gallery_revision", "0"))
    result = service.decide_group(group["id"], pet["id"], [excluded])
    assert result["saved"] == 2
    assert int(service.store.state("gallery_revision")) == revision + 1
    assert (
        service.store.one("SELECT status FROM reviews WHERE detection_id=?", (excluded,))["status"]
        == "pending"
    )
    assert service.store.one("SELECT count(*) n FROM reference_images")["n"] == 2
    service.rematch()
    assert (
        service.store.one("SELECT status FROM reviews WHERE detection_id=?", (excluded,))["status"]
        == "confirmed"
    )
    while service.publish_one():
        pass
    restarted = Service(service.settings, service.immich, service.detector, service.recognizer)
    undo = restarted.undo_group(result["undo_id"])
    assert undo == {"restored": 2, "automatic_restored": 1}
    assert restarted.store.one("SELECT count(*) n FROM reviews WHERE status='pending'")["n"] == 3
    assert restarted.store.one("SELECT count(*) n FROM reference_images")["n"] == 0
    while restarted.publish_one():
        pass
    assert all(not tags for tags in service.immich.assigned_tags.values())
    assert restarted.undo_info() is None


def test_skip_undo_does_not_revert_unrelated_automatic_results(service):
    rows = photos(service)
    pet = service.add_pet("Rusty", "dog")
    first = rows[0]["id"]
    service.store.execute(
        "UPDATE reviews SET status='confirmed',pet_id=? WHERE detection_id=?", (pet["id"], first)
    )
    service.store.execute(
        "INSERT INTO automatic_reviews VALUES (?,?)", (first, service.settings.recognizer_key)
    )
    service.store.execute(
        "INSERT INTO automatic_sources VALUES (?,?,?)",
        (first, service.settings.recognizer_key, "0"),
    )
    group = service.groups()[0]
    result = service.decide_group(group["id"], None, [], skip=True)
    assert (
        service.store.one("SELECT count(*) n FROM reference_images WHERE kind='negative'")["n"] == 2
    )
    assert service.undo_group(result["undo_id"])["automatic_restored"] == 0
    assert (
        service.store.one("SELECT status FROM reviews WHERE detection_id=?", (first,))["status"]
        == "confirmed"
    )


def test_stale_group_never_overwrites_a_human_decision(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    first = group["members"][0]["id"]
    service.decide(first, pet["id"], "confirmed", False)
    with pytest.raises(ValueError, match="group changed"):
        service.decide_group(group["id"], pet["id"], [])
    assert service.store.one("SELECT count(*) n FROM reviews WHERE status='confirmed'")["n"] == 1


def test_undo_refuses_later_feedback_and_has_no_partial_effect(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    other = service.add_pet("Wang Wang", "dog")
    group = service.groups()[0]
    result = service.decide_group(group["id"], pet["id"], [])
    service.add_reference(other["id"], group["members"][-1]["id"], "negative")
    with pytest.raises(ValueError, match="changed"):
        service.undo_group(result["undo_id"])
    assert service.store.one("SELECT count(*) n FROM reviews WHERE status='confirmed'")["n"] == 2


def test_group_actions_correct_species_and_can_restore_the_original_guess(service):
    photos(service, 2)
    service.store.execute("UPDATE detections SET species='cat'")
    pet = service.add_pet("Wang Wang", "dog")
    group = service.groups()[0]
    result = service.decide_group(group["id"], pet["id"], [])
    assert {row["species"] for row in service.store.rows("SELECT species FROM detections")} == {
        "dog"
    }
    service.undo_group(result["undo_id"])
    assert {row["species"] for row in service.store.rows("SELECT species FROM detections")} == {
        "cat"
    }


def test_groups_api_collections_and_cross_origin_controls(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        groups = client.get("/api/groups").json()
        assert groups["total_groups"] == 1 and groups["total_photos"] == 2
        group = groups["items"][0]
        assert "members" not in group
        detail = client.get("/api/groups/" + group["id"]).json()
        assert len(detail["members"]) == 2
        result = client.post(
            f"/api/groups/{group['id']}/decision", json={"pet_id": pet["id"]}
        ).json()
        service.publish_one()
        collection = client.get("/api/collections").json()[0]
        assert collection["photo_count"] == 1 and collection["cover_id"]
        assert client.post("/api/groups/undo/" + result["undo_id"]).status_code == 200
        assert client.post("/api/groups/undo/" + result["undo_id"]).status_code == 409
        assert (
            client.post(
                f"/api/groups/{group['id']}/decision", json={"excluded": ["invalid"]}
            ).status_code
            == 409
        )
    service.settings.auth_required = False
    with TestClient(create_app(service.settings, service)) as client:
        assert (
            client.post(
                f"/api/groups/{group['id']}/decision",
                json={"pet_id": pet["id"]},
                headers={"Origin": "https://other.example"},
            ).status_code
            == 403
        )


def test_group_undo_preserves_a_later_human_confirmation_of_an_automatic_result(service):
    photos(service)
    pet = service.add_pet("Rusty", "dog")
    service.settings.min_references = 1
    service.settings.auto_tag = True
    group = service.groups()[0]
    separate = group["members"][-1]["id"]
    result = service.decide_group(group["id"], pet["id"], [separate])
    service.rematch()
    service.decide(separate, pet["id"], "confirmed")
    assert service.undo_group(result["undo_id"])["automatic_restored"] == 0
    assert (
        service.store.one("SELECT status FROM reviews WHERE detection_id=?", (separate,))["status"]
        == "confirmed"
    )
    assert service.store.one("SELECT count(*) n FROM reference_images")["n"] == 1


def test_group_undo_refuses_a_replaced_asset_before_mutating_any_other_photo(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    result = service.decide_group(group["id"], pet["id"], [])
    service.store.execute(
        "UPDATE assets SET revision='new' WHERE id=?", (group["members"][-1]["asset_id"],)
    )
    with pytest.raises(ValueError, match="changed"):
        service.undo_group(result["undo_id"])
    assert service.store.one("SELECT count(*) n FROM reviews WHERE status='confirmed'")["n"] == 2


def test_newly_scanned_pending_photos_invalidate_the_group_cache(service):
    photos(service, 2)
    assert service.groups()[0]["photo_count"] == 2
    original = service.immich.items[0]
    service.immich.items.append({**original, "id": str(uuid4())})
    service.sync()
    service.process_one()
    assert service.groups()[0]["photo_count"] == 3


def test_a_published_photo_does_not_keep_asking_about_its_other_unassigned_animal(service):
    from immich_pet_recognition.models import Detection

    service.detector.items.append(Detection("dog", 0.9, (1, 1, 20, 20)))
    photos(service, 1)
    pet = service.add_pet("Wang Wang", "dog")
    first = service.store.one("SELECT id FROM detections")["id"]
    service.decide(first, pet["id"], "confirmed")
    service.rematch()
    assert service.groups()[0]["photo_count"] == 1
    service.publish_one()
    assert service.groups() == []
