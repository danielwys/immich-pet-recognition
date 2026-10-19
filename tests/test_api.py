from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from immich_pet_recognition.api import create_app
from immich_pet_recognition.config import Settings
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def test_private_data_and_controls_require_token(processed):
    with TestClient(create_app(processed.settings, processed)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        for path in ["/api/status", "/api/pets", "/api/detections", "/api/jobs"]:
            assert client.get(path).status_code == 401
        assert client.post("/api/sync").status_code == 401
        assert client.post("/api/review/batch", json={"items": []}).status_code == 401
        assert client.post("/api/references/batch", json={"items": []}).status_code == 401
        detection = processed.store.one("SELECT id FROM detections")["id"]
        assert client.get(f"/api/detections/{detection}/crop").status_code == 401
        assert client.get("/api/status", headers=AUTH).status_code == 200
        assert client.get("/api/status", headers=AUTH).json()["auth_required"] is True
        assert "test-api-key" not in client.get("/api/status", headers=AUTH).text


def test_ui_updates_do_not_mix_old_cached_scripts_with_new_markup(processed):
    with TestClient(create_app(processed.settings, processed)) as client:
        page = client.get("/")
        assert page.headers["cache-control"] == "no-store"
        assert "/static/app.js?v=" in page.text
        assert "/static/style.css?v=" in page.text
        script_url = page.text.split('src="')[1].split('"')[0]
        assert client.get(script_url).status_code == 200


def test_direct_access_keeps_real_data_and_controls_available_without_token(processed):
    processed.settings.auth_required = False
    with TestClient(create_app(processed.settings, processed)) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        assert response.json()["auth_required"] is False
        assert response.json()["demo"] is False
        assert "test-api-key" not in response.text
        assert "test-token" not in response.text
        assert client.get("/api/pets").status_code == 200
        detection = client.get("/api/detections").json()["items"][0]
        assert client.get(f"/api/detections/{detection['id']}/crop").status_code == 200
        assert (
            client.post(
                "/api/pets", json={"name": "Mochi"}, headers={"Origin": "http://testserver"}
            ).status_code
            == 201
        )
        assert client.post("/api/sync").status_code == 202


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://another-site.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_direct_access_rejects_changes_from_other_websites(processed, headers):
    processed.settings.auth_required = False
    with TestClient(create_app(processed.settings, processed)) as client:
        assert client.post("/api/sync", headers=headers).status_code == 403
        assert client.post("/api/pets", json={"name": "Mochi"}, headers=headers).status_code == 403
        assert not processed.sync_requested.is_set()
        assert client.get("/api/pets").json() == []


def test_real_deployments_must_explicitly_opt_out_of_token_authentication(tmp_path):
    with pytest.raises(ValueError, match="PET_SERVICE_TOKEN"):
        Settings(_env_file=None, service_token="", data_dir=tmp_path)
    settings = Settings(_env_file=None, auth_required=False, service_token="", data_dir=tmp_path)
    assert settings.auth_required is False


def test_enroll_review_reference_and_crop_flow(processed):
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        pet = client.post("/api/pets", json={"name": "Mochi", "species": "dog"}).json()
        assert client.post("/api/pets", json={"name": "mochi", "species": "dog"}).status_code == 409
        detection = client.get("/api/detections").json()["items"][0]
        assert (
            client.get(f"/api/detections/{detection['id']}/crop").headers["content-type"]
            == "image/jpeg"
        )
        assert (
            client.post(
                f"/api/pets/{pet['id']}/references",
                json={
                    "detection_id": detection["id"],
                    "kind": "positive",
                },
            ).status_code
            == 201
        )
        assert client.get("/api/pets").json()[0]["positive_count"] == 1
        response = client.post(
            f"/api/detections/{detection['id']}/decision",
            json={
                "status": "confirmed",
                "pet_id": pet["id"],
            },
        )
        assert response.status_code == 200
        assert client.get("/api/detections").json()["total"] == 0
        assert client.get("/api/detections?status=confirmed").json()["total"] == 1


def test_invalid_name_species_and_missing_pet_are_rejected(processed):
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert client.post("/api/pets", json={"name": " ", "species": "dog"}).status_code == 422
        assert (
            client.post("/api/pets", json={"name": "Pets/Mochi", "species": "dog"}).status_code
            == 422
        )
        pet = client.post("/api/pets", json={"name": "Cat", "species": "cat"}).json()
        detection = client.get("/api/detections").json()["items"][0]
        assert (
            client.post(
                f"/api/detections/{detection['id']}/decision",
                json={
                    "status": "confirmed",
                },
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/pets/{pet['id']}/references",
                json={
                    "detection_id": detection["id"],
                    "kind": "positive",
                },
            ).status_code
            == 400
        )


def test_demo_never_requests_immich_or_downloads_models(tmp_path):
    settings = Settings(_env_file=None, demo=True, data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/status").json()["demo"] is True
        assert client.get("/api/detections").json()["total"] == 6
        assert client.post("/api/sync").status_code == 202
        assert not (tmp_path / "models").exists()


def test_demo_cannot_open_real_photo_cache(processed):
    demo = processed.settings.model_copy(update={"demo": True})
    with pytest.raises(ValueError, match="separate"):
        Service(demo)


def add_another_photo(service):
    asset = {**service.immich.items[0], "id": str(uuid4()), "originalFileName": "another-dog.jpg"}
    service.immich.items.append(asset)
    service.sync()
    service.process_one()
    return service.store.one("SELECT id FROM detections WHERE asset_id=?", (asset["id"],))["id"]


def test_bulk_assignment_teaches_and_immediately_suggests_without_model_work(processed):
    second = add_another_photo(processed)
    first = processed.store.one("SELECT id FROM detections WHERE id<>?", (second,))["id"]
    pet = processed.add_pet("Mochi", "dog")
    calls = (processed.detector.calls, processed.recognizer.calls)
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert all(
            row["recommendation"] is None for row in client.get("/api/detections").json()["items"]
        )
        response = client.post(
            "/api/review/batch",
            json={
                "items": [
                    {
                        "detection_id": first,
                        "pet_id": pet["id"],
                        "status": "confirmed",
                        "learn": True,
                    }
                ]
            },
        )
        assert response.json()["saved"] == 1
        assert response.json()["failed"] == 0
        suggestion = client.get("/api/detections").json()["items"][0]["recommendation"]
        assert suggestion["pet_id"] == pet["id"]
        assert suggestion["score"] == pytest.approx(1)
        assert suggestion["references"] == 1
        assert suggestion["confident"] is False
        assert [gate["passed"] for gate in suggestion["gates"]] == [False, True, True]
        assert client.get("/api/pets").json()[0]["positive_count"] == 1
        assert client.get("/api/status").json()["suggestions_updating"] is True
        assert client.get("/api/status").json()["auto_tag"] is False
    assert (processed.detector.calls, processed.recognizer.calls) == calls
    assert (
        processed.store.one("SELECT status FROM reviews WHERE detection_id=?", (second,))["status"]
        == "pending"
    )


def test_bulk_partial_failures_allow_published_assignments_but_reject_invalid_rows(processed):
    second = add_another_photo(processed)
    first = processed.store.one("SELECT id FROM detections WHERE id<>?", (second,))["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(first, pet["id"], "confirmed")
    processed.publish_one()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        response = client.post(
            "/api/review/batch",
            json={
                "items": [
                    {
                        "detection_id": item,
                        "pet_id": pet["id"],
                        "status": "confirmed",
                        "learn": True,
                    }
                    for item in [first, second, "a" * 32]
                ]
            },
        )
        assert response.json()["saved"] == 2
        assert response.json()["failed"] == 1
        assert client.get("/api/pets").json()[0]["positive_count"] == 2
        duplicate = {"detection_id": second, "pet_id": pet["id"], "status": "confirmed"}
        assert (
            client.post("/api/review/batch", json={"items": [duplicate, duplicate]}).status_code
            == 422
        )


def test_existing_published_confirmations_can_become_examples_without_retagging(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(detection, pet["id"], "confirmed")
    processed.publish_one()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert client.get("/api/pets").json()[0]["positive_count"] == 0
        assert (
            client.post(
                "/api/references/batch",
                json={
                    "items": [{"detection_id": detection, "pet_id": pet["id"], "kind": "positive"}]
                },
            ).json()["saved"]
            == 1
        )
        assert client.get("/api/pets").json()[0]["positive_count"] == 1
    assert not processed.publish_one()
    assert len(processed.immich.tag_calls) == 1


def test_match_gates_and_pet_filter_follow_refreshed_ranking(processed):
    second = add_another_photo(processed)
    first = processed.store.one("SELECT id FROM detections WHERE id<>?", (second,))["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.settings.min_references = 1
    processed.decide(first, pet["id"], "confirmed", learn=True)
    processed.rematch()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        rows = client.get(f"/api/detections?pet_id={pet['id']}&sort=suggested").json()["items"]
        assert len(rows) == 1
        assert rows[0]["id"] == second
        assert rows[0]["recommendation"]["confident"] is True
        assert all(g["passed"] for g in rows[0]["recommendation"]["gates"])
        assert client.get("/api/status").json()["suggestions_updating"] is False
        assert rows[0]["status"] == "pending"


def test_rejecting_a_suggested_identity_keeps_photo_pending_and_removes_that_suggestion(processed):
    second = add_another_photo(processed)
    first = processed.store.one("SELECT id FROM detections WHERE id<>?", (second,))["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(first, pet["id"], "confirmed", learn=True)
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert client.get("/api/detections").json()["items"][0]["recommendation"] is not None
        response = client.post(
            "/api/references/batch",
            json={"items": [{"detection_id": second, "pet_id": pet["id"], "kind": "negative"}]},
        )
        assert response.json()["saved"] == 1
        row = client.get("/api/detections").json()["items"][0]
        assert row["recommendation"] is None
        assert row["status"] == "pending"


def test_bulk_correction_moves_published_tags_and_examples(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    butter = processed.add_pet("Butter", "dog")
    rusty = processed.add_pet("Rusty", "dog")
    processed.decide(detection, butter["id"], "confirmed", learn=True)
    processed.publish_one()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        result = client.post(
            "/api/review/batch",
            json={
                "items": [
                    {
                        "detection_id": detection,
                        "pet_id": rusty["id"],
                        "status": "confirmed",
                        "learn": True,
                        "expected_pet_id": butter["id"],
                    }
                ]
            },
        ).json()
        assert result["saved"] == 1
        assert client.get("/api/status").json()["pending_tag_removals"] == 1
        assert processed.publish_one()
        galleries = {p["name"]: p for p in client.get("/api/pets").json()}
        assert galleries["Butter"]["positive_count"] == 0
        assert galleries["Butter"]["negative_count"] == 1
        assert galleries["Rusty"]["positive_count"] == 1
        assert client.get("/api/status").json()["pending_tag_removals"] == 0
        row = client.get("/api/detections?status=confirmed").json()["items"][0]
        assert row["pet_id"] == rusty["id"]
        assert row["published"] == 1
        assert row["error"] is None
    asset_id = processed.immich.items[0]["id"]
    assert processed.immich.assigned_tags[asset_id] == {"Pets/Rusty"}


def test_stale_assignment_precondition_prevents_overwriting_another_correction(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    butter = processed.add_pet("Butter", "dog")
    rusty = processed.add_pet("Rusty", "dog")
    processed.decide(detection, rusty["id"], "confirmed", learn=True)
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        result = client.post(
            "/api/review/batch",
            json={
                "items": [
                    {
                        "detection_id": detection,
                        "pet_id": butter["id"],
                        "status": "confirmed",
                        "learn": True,
                        "expected_pet_id": butter["id"],
                    }
                ]
            },
        ).json()
        assert result["saved"] == 0
        assert result["failed"] == 1
        assert processed.store.one("SELECT pet_id FROM reviews")["pet_id"] == rusty["id"]
