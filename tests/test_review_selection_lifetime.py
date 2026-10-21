import json
import time

from fastapi.testclient import TestClient
from test_groups import photos

from immich_pet_recognition.api import create_app
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def restarted(service):
    return Service(service.settings, service.immich, service.detector, service.recognizer)


def test_cached_selection_still_saves_after_an_overnight_idle_page(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    service.group_snapshots["items"][group["id"]] = (time.monotonic() - 86400, group)
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        # Previously Refresh returned this exact cached group, but saving it
        # rejected its creation timestamp: an unrecoverable refresh loop.
        page = client.get("/api/groups").json()
        assert page["items"][0]["id"] == group["id"]
        response = client.post(
            "/api/groups/batch", json={"group_ids": [group["id"]], "pet_id": pet["id"]}
        )
        assert response.status_code == 200
        assert response.json()["saved"] == 2


def test_durable_selection_saves_after_restart_without_rebuilding_suggestions(service, monkeypatch):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    fresh = restarted(service)

    def must_not_regroup():
        raise AssertionError("Saving a known selection must not read the whole library")

    monkeypatch.setattr(fresh, "groups", must_not_regroup)
    with TestClient(create_app(fresh.settings, fresh), headers=AUTH) as client:
        detail = client.get("/api/groups/" + group["id"])
        assert detail.status_code == 200
        assert {row["id"] for row in detail.json()["members"]} == {
            row["id"] for row in group["members"]
        }
        response = client.post(
            "/api/groups/batch", json={"group_ids": [group["id"]], "pet_id": pet["id"]}
        )
        assert response.status_code == 200 and response.json()["saved"] == 2


def test_old_group_survives_regrouping_and_skips_real_assignment_or_asset_changes(service):
    rows = photos(service, 3)
    pet = service.add_pet("Rusty", "dog")
    other = service.add_pet("Butter", "dog")
    group = service.groups()[0]
    service.decide(rows[0]["id"], other["id"], "confirmed")
    service.store.execute(
        "UPDATE assets SET revision='replaced' WHERE id="
        "(SELECT asset_id FROM detections WHERE id=?)",
        (rows[1]["id"],),
    )
    fresh = restarted(service)
    assert group["id"] not in {g["id"] for g in fresh.groups()}
    with TestClient(create_app(fresh.settings, fresh), headers=AUTH) as client:
        detail = client.get("/api/groups/" + group["id"]).json()
        assert [row["id"] for row in detail["members"]] == [rows[2]["id"]]
        response = client.post(
            "/api/groups/" + group["id"] + "/decision", json={"pet_id": pet["id"]}
        )
        assert response.status_code == 200
        assert response.json()["saved"] == 1 and response.json()["skipped"] == 2
        assert (
            fresh.store.one("SELECT pet_id FROM reviews WHERE detection_id=?", (rows[0]["id"],))[
                "pet_id"
            ]
            == other["id"]
        )
        assert (
            fresh.store.one("SELECT status FROM reviews WHERE detection_id=?", (rows[1]["id"],))[
                "status"
            ]
            == "pending"
        )


def test_restoring_pre_upgrade_selections_recovers_an_old_page_without_changing_labels(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    payload = {
        "pipeline": service.settings.pipeline_key,
        "groups": [{"id": group["id"], "member_ids": [row["id"] for row in group["members"]]}],
    }
    service.store.execute("DELETE FROM review_selections")
    fresh = restarted(service)
    before = fresh.store.rows("SELECT * FROM reviews")
    with TestClient(create_app(fresh.settings, fresh)) as client:
        assert client.post("/api/groups/selections", json=payload).status_code == 401
        response = client.post("/api/groups/selections", json=payload, headers=AUTH)
        assert response.status_code == 200 and response.json() == {"restored": 1}
        assert fresh.store.rows("SELECT * FROM reviews") == before
        assert fresh.store.rows("SELECT * FROM reference_images") == []
        response = client.post(
            "/api/groups/batch",
            json={"group_ids": [group["id"]], "pet_id": pet["id"]},
            headers=AUTH,
        )
        assert response.status_code == 200 and response.json()["saved"] == 2


def test_mismatched_or_different_pipeline_selection_cannot_be_registered(service):
    photos(service, 2)
    group = service.groups()[0]
    valid = {"id": group["id"], "member_ids": [row["id"] for row in group["members"]]}
    service.store.execute("DELETE FROM review_selections")
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        assert (
            client.post(
                "/api/groups/selections", json={"pipeline": "other model", "groups": [valid]}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/groups/selections",
                json={
                    "pipeline": service.settings.pipeline_key,
                    "groups": [valid, {**valid, "id": "0" * 24}],
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/groups/selections",
                json={
                    "pipeline": service.settings.pipeline_key,
                    "groups": [{**valid, "member_ids": ["not-a-photo"]}],
                },
            ).status_code
            == 422
        )
    assert service.store.rows("SELECT * FROM review_selections") == []


def test_durable_selection_records_contain_only_members_not_old_names_or_scores(service):
    photos(service, 2)
    pet = service.add_pet("Rusty", "dog")
    group = service.groups()[0]
    saved = service.store.one("SELECT * FROM review_selections")
    assert set(saved) == {"id", "model", "detector", "members"}
    assert set(json.loads(saved["members"])) == {row["id"] for row in group["members"]}
    service.decide_group(group["id"], pet["id"], [])
    assert service.store.one("SELECT * FROM review_selections") == saved
