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


def test_manual_identifications_automatically_teach_without_a_separate_enrollment_step(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(detection, pet["id"], "confirmed")
    processed.publish_one()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert client.get("/api/pets").json()[0]["positive_count"] == 1
        assert (
            client.post(
                f"/api/detections/{detection}/decision",
                json={"status": "confirmed", "pet_id": pet["id"]},
            ).status_code
            == 200
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


def test_automation_controls_and_audit_filter(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.settings.min_references = 1
    processed.add_reference(pet["id"], detection, "positive")
    with TestClient(create_app(processed.settings, processed)) as client:
        path = f"/api/pets/{pet['id']}/automation"
        assert client.put(path, json={"enabled": True}).status_code == 401
        assert (
            client.put(
                f"/api/pets/{uuid4()}/automation", json={"enabled": True}, headers=AUTH
            ).status_code
            == 404
        )
        response = client.put(path, json={"enabled": True}, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["auto_tag"] is True
        assert client.get("/api/status", headers=AUTH).json()["auto_tag"] is True
        processed.rematch()
        processed.publish_one()
        auto = client.get("/api/detections?status=confirmed&source=automatic", headers=AUTH).json()
        assert auto["total"] == 1
        assert auto["items"][0]["automatic"] == 1
        assert client.get("/api/pets", headers=AUTH).json()[0]["auto_tagged_count"] == 1
        client.put(path, json={"enabled": False}, headers=AUTH)
        assert client.get("/api/status", headers=AUTH).json()["auto_tag"] is False
        assert (
            client.get("/api/detections?status=confirmed&source=automatic", headers=AUTH).json()[
                "total"
            ]
            == 1
        )
        processed.settings.auth_required = False
        assert (
            client.put(
                path, json={"enabled": True}, headers={"Origin": "https://other.example"}
            ).status_code
            == 403
        )


def test_tagged_view_combines_manual_and_automatic_and_waits_for_publication(processed):
    first = processed.store.one("SELECT id FROM detections")["id"]
    add_another_photo(processed)
    pet = processed.add_pet("Rusty", "dog")
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:

        def view(tagging, **extra):
            return client.get(
                "/api/detections", params={"status": "all", "tagging": tagging, **extra}
            ).json()

        assert view("untagged")["total"] == 2
        assert view("tagged")["total"] == 0
        processed.decide(first, pet["id"], "confirmed")
        assert view("untagged")["total"] == 2  # Queued and failed writes are still untagged.
        processed.immich.fail_tag = True
        processed.publish_one()
        assert view("tagged")["total"] == 0
        processed.immich.fail_tag = False
        processed.retry_failed()
        processed.publish_one()
        processed.settings.min_references = 1
        processed.add_reference(pet["id"], first, "positive")
        processed.set_automation(pet["id"], True)
        processed.rematch()
        assert view("untagged")["total"] == 1
        processed.publish_one()
        tagged = view("tagged")
        assert tagged["total"] == tagged["photo_total"] == 2
        assert {item["automatic"] for item in tagged["items"]} == {0, 1}
        assert all(
            item["photo_tags"] == [{"pet_id": pet["id"], "name": "Rusty", "tag": "Pets/Rusty"}]
            for item in tagged["items"]
        )
        assert view("tagged", source="automatic")["total"] == 1
        assert view("tagged", source="manual")["total"] == 1
        assert view("untagged")["total"] == 0


def test_tagged_wang_wang_never_includes_rusty_suggestions_from_the_same_photo(processed):
    from immich_pet_recognition.models import Detection

    first = processed.store.one("SELECT id FROM detections")["id"]
    wang = processed.add_pet("Wang Wang", "dog")
    rusty = processed.add_pet("Rusty", "dog")
    processed.decide(first, wang["id"], "confirmed")
    processed.publish_one()
    processed.detector.items.append(Detection("dog", 0.9, (1, 1, 20, 20)))
    processed.store.execute("DELETE FROM passes")
    processed.store.execute("UPDATE jobs SET status='queued'")
    processed.process_one()
    second = processed.store.one("SELECT id FROM detections WHERE id<>?", (first,))["id"]
    import numpy as np

    processed.store.save_embedding(
        second, processed.settings.recognizer_key, np.array([0, 1, 0], dtype=np.float32)
    )
    processed.add_reference(rusty["id"], second, "positive")
    processed.rematch()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        assert client.get("/api/detections?status=all&tagging=untagged").json()["total"] == 0
        tagged = client.get(f"/api/detections?status=all&tagging=tagged&pet_id={wang['id']}").json()
        assert tagged["total"] == tagged["photo_total"] == 1
        assert tagged["items"][0]["id"] == first
        assert tagged["items"][0]["pet_name"] == "Wang Wang"
        assert tagged["items"][0]["recommendation"] is None
        assert tagged["items"][0]["published"] == 1
        assert (
            client.get(f"/api/detections?status=all&tagging=tagged&pet_id={rusty['id']}").json()[
                "total"
            ]
            == 0
        )
        unfinished = client.get("/api/detections?status=all&tagging=unassigned").json()
        assert unfinished["total"] == 1
        assert unfinished["items"][0]["recommendation"]["pet_name"] == "Rusty"
        assert unfinished["items"][0]["photo_tags"][0]["name"] == "Wang Wang"


def test_pending_tag_changes_are_not_shown_as_completed_or_completely_untagged(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    rusty = processed.add_pet("Rusty", "dog")
    butter = processed.add_pet("Butter", "dog")
    processed.decide(detection, rusty["id"], "confirmed")
    processed.publish_one()
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:

        def view(tagging):
            return client.get(
                "/api/detections", params={"status": "all", "tagging": tagging}
            ).json()

        processed.decide(detection, butter["id"], "confirmed")
        assert view("tagged")["total"] == 0
        assert view("untagged")["total"] == 0
        assert view("all")["items"][0]["photo_tags"][0]["name"] == "Rusty"
        processed.publish_one()
        assert view("tagged")["items"][0]["photo_tags"][0]["name"] == "Butter"
        processed.decide(detection, None, "rejected")
        assert view("tagged")["total"] == 0
        assert view("untagged")["total"] == 0
        assert view("all")["items"][0]["photo_tags"][0]["name"] == "Butter"
        processed.publish_one()
        assert view("tagged")["total"] == 0
        assert view("untagged")["total"] == 1
        assert (
            client.get(
                "/api/detections?status=all&tagging=untagged&include_dismissed=false"
            ).json()["total"]
            == 0
        )


def test_demo_assignment_moves_to_tagged_without_writing_to_immich(tmp_path):
    settings = Settings(_env_file=None, demo=True, data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        pet = client.get("/api/pets").json()[0]
        crop = client.get("/api/detections").json()["items"][0]
        client.post(
            f"/api/detections/{crop['id']}/decision",
            json={
                "status": "confirmed",
                "pet_id": pet["id"],
            },
        )
        tagged = client.get("/api/detections?status=all&tagging=tagged").json()
        assert tagged["total"] == 1
        assert tagged["items"][0]["photo_tags"][0]["name"] == pet["name"]
        summary = client.get("/api/status").json()["review_summary"]
        assert summary["tagged_photos"] == summary["tagged_crops"] == 1
        assert summary["untagged_photos"] == summary["untagged_crops"] == 5
        assert summary["pending_tag_changes"] == 0


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("learn", [False, True])
def test_human_assignment_overrides_wrong_species_in_single_and_bulk_review(processed, bulk, learn):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    processed.store.execute("UPDATE detections SET species='cat' WHERE id=?", (detection,))
    cat = processed.add_pet("Cat example", "cat")
    wang = processed.add_pet("Wang Wang", "dog")
    processed.add_reference(cat["id"], detection, "positive")
    revision = processed.store.state("gallery_revision")
    body = {"status": "confirmed", "pet_id": wang["id"], "learn": learn}
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        if bulk:
            response = client.post(
                "/api/review/batch", json={"items": [{"detection_id": detection, **body}]}
            )
            assert response.json()["saved"] == 1
            assert response.json()["failed"] == 0
        else:
            response = client.post(f"/api/detections/{detection}/decision", json=body)
        assert response.status_code == 200
        assert processed.store.one("SELECT species FROM detections")["species"] == "dog"
        assert processed.store.one("SELECT pet_id,status FROM reviews") == {
            "pet_id": wang["id"],
            "status": "confirmed",
        }
        pets = {pet["name"]: pet for pet in client.get("/api/pets").json()}
        assert (
            pets["Wang Wang"]["positive_count"] == 1
        )  # Legacy learn flags no longer change naming.
        assert pets["Cat example"]["positive_count"] == 0
        assert processed.store.state("gallery_revision") != revision
        assert processed.publish_one()
        tagged = client.get("/api/detections?status=all&tagging=tagged").json()["items"][0]
        assert tagged["species"] == "dog"
        assert tagged["pet_name"] == "Wang Wang"
        assert tagged["published"] == 1


def test_invalid_or_stale_species_correction_leaves_the_crop_unchanged(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    processed.store.execute("UPDATE detections SET species='cat' WHERE id=?", (detection,))
    wang = processed.add_pet("Wang Wang", "dog")
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:
        for body in [
            {"status": "confirmed", "pet_id": str(uuid4())},
            {"status": "confirmed", "pet_id": wang["id"], "expected_pet_id": str(uuid4())},
        ]:
            assert (
                client.post(f"/api/detections/{detection}/decision", json=body).status_code == 400
            )
        assert processed.store.one("SELECT species FROM detections")["species"] == "cat"
        assert processed.store.one("SELECT status FROM reviews")["status"] == "pending"
        assert not processed.publish_one()


def test_summary_counts_photos_not_crops_and_excludes_already_tagged_animals(processed):
    from immich_pet_recognition.models import Detection

    processed.detector.items.append(Detection("dog", 0.9, (1, 1, 20, 20)))
    processed.store.execute("DELETE FROM passes")
    processed.store.execute("UPDATE jobs SET status='queued'")
    processed.process_one()
    rows = processed.store.rows("SELECT id FROM detections")
    pet = processed.add_pet("Rusty", "dog")
    with TestClient(create_app(processed.settings, processed), headers=AUTH) as client:

        def summary():
            result = client.get("/api/status").json()["review_summary"]
            for kind in ("untagged", "tagged"):
                facet = client.get(
                    "/api/detections",
                    params={"tagging": kind, "status": "all", "include_dismissed": False},
                ).json()
                assert result[f"{kind}_photos"] == facet["photo_total"]
                assert result[f"{kind}_crops"] == facet["total"]
            return result

        counts = summary()
        assert counts["untagged_photos"] == 1 and counts["untagged_crops"] == 2
        processed.decide(rows[0]["id"], pet["id"], "confirmed")
        assert summary()["untagged_photos"] == 1
        assert summary()["pending_tag_changes"] == 1
        processed.immich.fail_tag = True
        processed.publish_one()
        assert summary()["tagged_photos"] == 0
        processed.immich.fail_tag = False
        processed.retry_failed()
        processed.publish_one()
        counts = summary()
        assert counts["untagged_photos"] == counts["untagged_crops"] == 0
        assert counts["tagged_photos"] == counts["tagged_crops"] == 1
        assert counts["partially_tagged_photos"] == counts["extra_pending_crops"] == 1
        processed.decide(rows[0]["id"], None, "rejected")
        assert summary()["pending_tag_changes"] == 1
        assert summary()["untagged_photos"] == 0  # Old tag still exists during removal.
        processed.publish_one()
        assert summary()["untagged_photos"] == summary()["untagged_crops"] == 1
        processed.decide(rows[1]["id"], None, "rejected")
        assert summary()["untagged_photos"] == summary()["untagged_crops"] == 0
        # Old asset revisions and other model results must never inflate the count.
        processed.store.execute("UPDATE assets SET revision='changed'")
        assert summary()["tagged_photos"] == summary()["untagged_photos"] == 0


@pytest.mark.parametrize(
    "candidate,examples,score,margin,enabled,reason",
    [
        (False, 0, 0, 0, False, "no_candidate"),
        (True, 0, 1, 1, True, "needs_examples"),
        (True, 1, 0.84999, 1, True, "below_similarity"),
        (True, 1, 0.85, 0.07999, True, "ambiguous"),
        (True, 1, 0.85, 0.08, False, "automation_paused"),
        (True, 1, 0.85, 0.08, True, "ready"),
    ],
)
def test_summary_explains_each_pending_crop_once(
    processed, candidate, examples, score, margin, enabled, reason
):
    processed.settings.min_references = 1
    pet = processed.add_pet("Rusty", "dog")
    detection = processed.store.one("SELECT id FROM detections")["id"]
    if examples:
        processed.add_reference(pet["id"], detection, "positive")
    processed.set_automation(pet["id"], enabled)
    processed.store.execute(
        "UPDATE reviews SET pet_id=?,score=?,margin=?",
        (pet["id"] if candidate else None, score, margin),
    )
    assert processed.review_summary()["reasons"] == {reason: 1}


def test_summary_scales_to_a_library_without_rebuilding_the_tag_set_per_crop(service, monkeypatch):
    from contextlib import contextmanager

    pet = service.add_pet("Rusty", "dog")
    model, detector = service.settings.recognizer_key, service.settings.detector_key
    with service.store.connection() as conn:
        conn.executemany(
            "INSERT INTO assets VALUES (?,?,?)",
            [(f"asset-{i}", "v1", "test.jpg") for i in range(5000)],
        )
        conn.executemany(
            "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?)",
            [
                (f"crop-{i}", f"asset-{i // 2}", "v1", detector, "dog", 0.9, "[]", "")
                for i in range(10000)
            ],
        )
        conn.executemany(
            "INSERT INTO reviews(detection_id,model,pet_id,score,margin,status,published) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    f"crop-{i}",
                    model,
                    pet["id"] if i % 20 == 0 else None,
                    0,
                    0,
                    "confirmed" if i % 20 == 0 else "pending",
                    int(i % 20 == 0),
                )
                for i in range(10000)
            ],
        )
    connection = service.store.connection
    steps = 0

    def budget():
        nonlocal steps
        steps += 1
        return steps > 10000  # Bound SQL work, not wall time on a shared CI runner.

    @contextmanager
    def bounded_connection():
        with connection() as conn:
            conn.set_progress_handler(budget, 1000)
            yield conn

    monkeypatch.setattr(service.store, "connection", bounded_connection)
    summary = service.review_summary()
    assert summary["untagged_photos"] == 4500
    assert summary["untagged_crops"] == 9000
    assert summary["tagged_photos"] == summary["tagged_crops"] == 500
    assert summary["partially_tagged_photos"] == summary["extra_pending_crops"] == 500
