from uuid import uuid4

from fastapi.testclient import TestClient

from immich_pet_recognition.api import create_app
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def test_album_cover_persists_without_teaching_and_ignores_later_reassignment(processed):
    service = processed
    first = service.store.one("SELECT id FROM detections")["id"]
    service.immich.items.append({**service.immich.items[0], "id": str(uuid4())})
    service.sync()
    assert service.process_one()
    second = next(
        row["id"] for row in service.store.rows("SELECT id FROM detections") if row["id"] != first
    )
    rusty = service.add_pet("Rusty", "dog")
    butter = service.add_pet("Butter", "dog")
    service.decide(first, rusty["id"], "confirmed")
    service.decide(second, rusty["id"], "confirmed")
    while service.publish_one():
        pass

    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        references = service.store.rows("SELECT * FROM reference_images ORDER BY detection_id")
        reviews = service.store.rows("SELECT * FROM reviews ORDER BY detection_id")
        revision = service.store.state("gallery_revision")
        response = client.put(f"/api/pets/{rusty['id']}/cover", json={"detection_id": second})
        assert response.status_code == 200
        assert response.json() == {"cover_id": second}
        assert service.store.state("gallery_revision") == revision
        assert (
            service.store.rows("SELECT * FROM reference_images ORDER BY detection_id") == references
        )
        assert service.store.rows("SELECT * FROM reviews ORDER BY detection_id") == reviews

    restarted = Service(service.settings, service.immich, service.detector, service.recognizer)
    with TestClient(create_app(service.settings, restarted), headers=AUTH) as client:
        albums = {pet["id"]: pet for pet in client.get("/api/collections").json()}
        assert albums[rusty["id"]]["cover_id"] == second

        restarted.decide(second, butter["id"], "confirmed")
        while restarted.publish_one():
            pass
        albums = {pet["id"]: pet for pet in client.get("/api/collections").json()}
        assert albums[rusty["id"]]["cover_id"] == first
        assert albums[butter["id"]]["cover_id"] == second
        assert (
            client.put(f"/api/pets/{rusty['id']}/cover", json={"detection_id": second}).status_code
            == 409
        )
