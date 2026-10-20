import io

from fastapi.testclient import TestClient
from PIL import Image

from immich_pet_recognition.api import create_app
from immich_pet_recognition.models import Detection

AUTH = {"Authorization": "Bearer test-token"}


def test_photo_view_is_full_immich_preview_authenticated_and_shared_across_crops(service):
    service.detector.items.append(Detection("dog", 0.9, (1, 1, 20, 20)))
    service.sync()
    service.process_one()
    detections = service.store.rows("SELECT id FROM detections")
    initial_calls = service.immich.preview_calls
    with TestClient(create_app(service.settings, service)) as client:
        path = f"/api/detections/{detections[0]['id']}/photo?size=320"
        assert client.get(path).status_code == 401
        assert service.immich.preview_calls == initial_calls
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200
        assert response.headers["vary"] == "Authorization"
        with Image.open(io.BytesIO(response.content)) as full:
            assert full.size == (100, 80)
        with Image.open(
            io.BytesIO(
                client.get(f"/api/detections/{detections[0]['id']}/crop", headers=AUTH).content
            )
        ) as crop:
            assert crop.size != (100, 80)
        other = client.get(f"/api/detections/{detections[1]['id']}/photo?size=320", headers=AUTH)
        assert other.content == response.content
        assert service.immich.preview_calls == initial_calls + 1
        assert client.get(path.replace("320", "1601"), headers=AUTH).status_code == 422
        service.store.execute("UPDATE assets SET revision='replaced'")
        assert client.get(path, headers=AUTH).status_code == 404


def test_full_photo_does_not_fetch_hidden_or_changed_immich_assets(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    processed.immich.items[0]["visibility"] = "hidden"
    calls = processed.immich.preview_calls
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert client.get(f"/api/detections/{detection}/photo").status_code == 404
    assert processed.immich.preview_calls == calls


def test_album_deduplicates_photos_and_removes_only_its_own_identity(service):
    service.detector.items.extend(
        [Detection("dog", 0.9, (1, 1, 20, 20)), Detection("dog", 0.9, (30, 20, 50, 40))]
    )
    service.sync()
    service.process_one()
    ids = [row["id"] for row in service.store.rows("SELECT id FROM detections")]
    rusty = service.add_pet("Rusty", "dog")
    butter = service.add_pet("Butter", "dog")
    for did in ids[:2]:
        service.decide(did, rusty["id"], "confirmed")
    service.decide(ids[2], butter["id"], "confirmed")
    # Albums show a name immediately, before Immich has published it.
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        query = f"/api/detections?status=confirmed&pet_id={rusty['id']}&unique_photos=true"
        data = client.get(query).json()
        assert data["total"] == data["photo_total"] == 1 and data["crop_total"] == 2
        row = data["items"][0]
        assert set(row["assignment_ids"]) == set(ids[:2])
        assert {p["name"] for p in row["identities"]} == {"Rusty", "Butter"}
        assert client.get(query.replace("&unique_photos=true", "")).json()["total"] == 2
        result = client.post(
            "/api/review/batch",
            json={
                "items": [
                    {
                        "detection_id": did,
                        "status": "pending",
                        "expected_pet_id": rusty["id"],
                        "expected_status": "confirmed",
                    }
                    for did in row["assignment_ids"]
                ]
            },
        )
        assert result.json()["saved"] == 2
        assert client.get(query).json()["total"] == 0
        assert service.store.one(
            "SELECT pet_id,status FROM reviews WHERE detection_id=?", (ids[2],)
        ) == {"pet_id": butter["id"], "status": "confirmed"}


def test_album_source_filter_still_collects_all_of_that_photos_current_identity(service):
    service.detector.items.append(Detection("dog", 0.9, (1, 1, 20, 20)))
    service.sync()
    service.process_one()
    ids = [row["id"] for row in service.store.rows("SELECT id FROM detections")]
    pet = service.add_pet("Rusty", "dog")
    for did in ids:
        service.decide(did, pet["id"], "confirmed")
    service.store.execute(
        "INSERT INTO automatic_reviews VALUES (?,?)", (ids[0], service.settings.recognizer_key)
    )
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        response = client.get(
            f"/api/detections?status=confirmed&pet_id={pet['id']}&source=manual&unique_photos=true"
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert set(response.json()["items"][0]["assignment_ids"]) == set(ids)


def test_pet_suggestions_are_plausible_and_relevance_ordered_without_changing_automation(service):
    import numpy as np
    from test_groups import photos

    rows = photos(service, 4)
    pet = service.add_pet("Wang Wang", "dog")
    for index, row in enumerate(rows):
        vector = np.eye(3, dtype=np.float32)[index // 2]
        service.store.save_embedding(row["id"], service.settings.recognizer_key, vector)
        service.store.execute(
            "UPDATE reviews SET pet_id=?,score=? WHERE detection_id=?",
            (pet["id"], [0.82, 0.81, 0.35, 0.34][index], row["id"]),
        )
    before = service.store.rows("SELECT * FROM reviews ORDER BY detection_id")
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        all_groups = client.get("/api/groups?pet_id=" + pet["id"]).json()
        relevant = client.get("/api/groups?pet_id=" + pet["id"] + "&near_match=true").json()
        assert all_groups["total_photos"] == 4 and relevant["total_photos"] == 2
        assert relevant["items"][0]["samples"][0]["score"] >= 0.81
        assert service.store.rows("SELECT * FROM reviews ORDER BY detection_id") == before
        assert client.get("/api/groups").json()["total_photos"] == 4
