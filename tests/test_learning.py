import numpy as np
from fastapi.testclient import TestClient

from immich_pet_recognition.api import create_app
from immich_pet_recognition.learning import fit_policies
from immich_pet_recognition.matching import match_pet
from immich_pet_recognition.service import Service


def galleries():
    rng = np.random.default_rng(7)
    pets = []
    for index, name in enumerate(("Rusty", "Wang Wang", "Butter")):
        center = np.eye(16, dtype=np.float32)[index]
        vectors = center + rng.normal(0, 0.24, (20, 16))
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        pets.append(
            {
                "id": name,
                "species": "dog",
                "positive": list(vectors),
                "negative": [],
                "positive_ids": [f"{name}-{i}" for i in range(20)],
                "positive_assets": [f"{name}-photo-{i}" for i in range(20)],
                "negative_ids": [],
                "negative_assets": [],
            }
        )
    return pets


def test_learned_policy_accepts_separable_known_pets_below_old_cutoff():
    gallery = galleries()
    policies = fit_policies(gallery, 0.85, 0.08, 10)
    assert all(policy["state"] == "fitted" for policy in policies.values())
    assert all(policy["threshold"] < 0.85 for policy in policies.values())
    for policy in policies.values():
        assert policy["positive_checked"] == 20 and policy["other_checked"] == 40
        assert policy["accepted_correct"] >= 10
        assert policy["accepted_incorrect"] == 0
    heldout = np.eye(16, dtype=np.float32)[0] * 0.8 + np.eye(16, dtype=np.float32)[15] * 0.6
    strict = match_pet(heldout, "dog", gallery, 0.85, 0.08, 10)
    learned = match_pet(heldout, "dog", gallery, 0.85, 0.08, 10, policies)
    assert learned.pet_id == "Rusty" and learned.confident
    assert learned.score < 0.85 and not strict.confident


def test_entire_query_photo_is_excluded_and_a_single_burst_does_not_look_validated():
    gallery = galleries()
    for pet in gallery:
        pet["positive_assets"] = [pet["id"] + "-one-photo"] * 20
    policies = fit_policies(gallery, 0.85, 0.08, 10)
    assert all(policy["positive_checked"] == 1 for policy in policies.values())
    assert all(policy["state"] == "starting" for policy in policies.values())


def test_indistinguishable_labels_do_not_earn_automatic_assignments():
    gallery = galleries()
    same = np.eye(16, dtype=np.float32)[0]
    for pet in gallery:
        pet["positive"] = [same.copy() for _ in range(20)]
    policies = fit_policies(gallery, 0.85, 0.08, 10)
    assert all(policy["state"] == "overlap" for policy in policies.values())
    assert not match_pet(same, "dog", gallery, 0.85, 0.08, 10, policies).confident


def test_explicit_negative_is_used_as_contrast_without_becoming_another_pet_label():
    gallery = galleries()[:1]
    pet = gallery[0]
    pet["negative"] = list(np.eye(16, dtype=np.float32)[1:5])
    pet["negative_ids"] = [f"not-{i}" for i in range(4)]
    pet["negative_assets"] = [f"other-photo-{i}" for i in range(4)]
    policy = fit_policies(gallery, 0.85, 0.08, 10)[pet["id"]]
    assert policy["state"] == "fitted"
    assert policy["other_checked"] == 4 and policy["accepted_incorrect"] == 0


def test_adaptive_preview_is_read_only_and_activation_survives_restart(processed):
    pet = processed.add_pet("Rusty", "dog")
    with TestClient(
        create_app(processed.settings, processed), headers={"Authorization": "Bearer test-token"}
    ) as client:
        revision = processed.store.state("gallery_revision")
        preview = client.get("/api/matching?mode=adaptive").json()
        assert preview["mode"] == "adaptive" and preview["active"]["mode"] == "fixed"
        assert preview["pets"][0]["policy"]["threshold"] == 0.75
        assert processed.store.state("gallery_revision") == revision
        response = client.put("/api/matching", json={"mode": "adaptive", "profile": "balanced"})
        assert response.status_code == 200
        assert processed.recheck_status()["state"] == "queued"
        restarted = Service(
            processed.settings, processed.immich, processed.detector, processed.recognizer
        )
        assert restarted.matching_options() == {"mode": "adaptive", "profile": "balanced"}
        assert restarted.policies()[pet["id"]]["threshold"] == 0.75
        assert client.put("/api/matching", json={"profile": "reckless"}).status_code == 422


def test_older_manual_names_are_adopted_but_automatic_results_never_teach(processed):
    pet = processed.add_pet("Rusty", "dog")
    row = processed.store.one("SELECT id FROM detections")
    processed.decide(row["id"], pet["id"], "confirmed", learn=False)
    assert processed.store.one("SELECT count(*) n FROM reference_images")["n"] == 0
    processed.adopt_manual_identifications()
    assert processed.store.one("SELECT count(*) n FROM reference_images")["n"] == 1
    revision = processed.store.state("gallery_revision")
    processed.adopt_manual_identifications()
    assert processed.store.state("gallery_revision") == revision
    processed.store.execute("DELETE FROM reference_images")
    processed.store.execute(
        "INSERT INTO automatic_reviews VALUES (?,?)", (row["id"], processed.settings.recognizer_key)
    )
    processed.adopt_manual_identifications()
    assert processed.store.one("SELECT count(*) n FROM reference_images")["n"] == 0


def test_naming_a_pet_turns_on_automatic_sorting_without_an_enrollment_control(processed):
    with TestClient(
        create_app(processed.settings, processed), headers={"Authorization": "Bearer test-token"}
    ) as client:
        pet = client.post("/api/pets", json={"name": "Wang Wang", "species": "dog"}).json()
        assert client.get("/api/pets").json()[0]["auto_tag"] is True
        detection = processed.store.one("SELECT id FROM detections")["id"]
        assert (
            client.post(
                f"/api/detections/{detection}/decision",
                json={"status": "confirmed", "pet_id": pet["id"], "learn": False},
            ).status_code
            == 200
        )
        assert client.get("/api/pets").json()[0]["positive_count"] == 1
        page = client.get("/").text
        assert "learn-from-assignments" not in page
        assert "Use as example" not in page
        assert "validation" not in page
        assert "training" not in page
