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
        detection = processed.store.one("SELECT id FROM detections")["id"]
        assert client.get(f"/api/detections/{detection}/crop").status_code == 401
        assert client.get("/api/status", headers=AUTH).status_code == 200
        assert client.get("/api/status", headers=AUTH).json()["auth_required"] is True
        assert "test-api-key" not in client.get("/api/status", headers=AUTH).text


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
