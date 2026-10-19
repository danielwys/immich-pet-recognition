import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import numpy as np
import pytest

from immich_pet_recognition.immich import ImmichError, asset_revision
from immich_pet_recognition.matching import match_pet
from immich_pet_recognition.service import Service


def test_sync_deduplicates_jobs(processed):
    assert processed.sync(full=True) == 0
    assert not processed.process_one()
    assert processed.detector.calls == 1
    assert processed.recognizer.calls == 1


def test_partial_sync_does_not_advance_checkpoint(service):
    service.store.set_state("sync_watermark", "2026-10-01T00:00:00+00:00")

    def broken_page(since=None):
        yield service.immich.items[0]
        raise ImmichError("Page two failed")

    service.immich.assets = broken_page
    with pytest.raises(ImmichError):
        service.sync()
    assert service.store.state("sync_watermark") == "2026-10-01T00:00:00+00:00"
    assert service.store.one("SELECT count(*) n FROM jobs")["n"] == 1


def test_queue_claim_is_atomic(service):
    service.sync()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(
            pool.map(lambda _: service.store.claim(service.settings.pipeline_key), range(2))
        )
    assert sum(job is not None for job in jobs) == 1


def test_background_worker_processes_and_publishes_confirmation(service):
    service.settings.background = True
    pet = service.add_pet("Mochi", "dog")
    service.start()
    try:
        deadline = time.monotonic() + 3
        while not service.store.one("SELECT 1 FROM reviews") and time.monotonic() < deadline:
            time.sleep(0.01)
        detection = service.store.one("SELECT id FROM detections")["id"]
        service.decide(detection, pet["id"], "confirmed")
        deadline = time.monotonic() + 3
        while not service.immich.tag_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.immich.tag_calls == [(service.immich.items[0]["id"], "Pets/Mochi")]
    finally:
        service.close()


def test_tag_metadata_does_not_change_revision(service):
    asset = service.immich.items[0]
    previous = asset_revision(asset)
    asset["updatedAt"] = "later"
    assert asset_revision(asset) == previous
    asset["checksum"] = "new-original"
    assert asset_revision(asset) != previous


def test_empty_detection_pass_is_cached(service):
    service.detector.items = []
    service.sync()
    service.process_one()
    service.settings.recognizer_revision = "another-recognizer"
    service.sync(full=True)
    service.process_one()
    assert service.detector.calls == 1
    assert service.recognizer.calls == 0


def test_new_recognizer_reuses_crops_and_reembeds_references(processed):
    detection = processed.store.one("SELECT * FROM detections")
    pet = processed.add_pet("Mochi", "dog")
    processed.add_reference(pet["id"], detection["id"], "positive")
    processed.settings.recognizer_revision = "replacement"
    processed.sync(full=True)
    processed.process_one()
    processed.rematch()
    assert processed.detector.calls == 1
    assert processed.immich.preview_calls == 1
    assert processed.recognizer.calls == 2
    assert processed.store.one("SELECT count(*) n FROM embeddings")["n"] == 2
    assert len(processed.references()[0]["positive"]) == 1


def test_retry_reuses_detection_pass(service):
    service.recognizer.fail = True
    service.sync()
    service.process_one()
    assert service.store.one("SELECT status FROM jobs")["status"] == "queued"
    service.recognizer.fail = False
    service.store.execute("UPDATE jobs SET available_at=0")
    service.process_one()
    assert service.detector.calls == 1
    assert service.immich.preview_calls == 1
    assert service.store.one("SELECT status FROM jobs")["status"] == "completed"


def test_jobs_fail_after_three_attempts_and_can_be_retried(service):
    service.recognizer.fail = True
    service.sync()
    for _ in range(3):
        service.store.execute("UPDATE jobs SET available_at=0")
        service.process_one()
    row = service.store.one("SELECT * FROM jobs")
    assert (row["status"], row["attempts"]) == ("failed", 3)
    service.retry_failed()
    assert service.store.one("SELECT status,attempts FROM jobs") == {
        "status": "queued",
        "attempts": 0,
    }


def test_restart_recovers_interrupted_job(service):
    service.sync()
    service.store.claim(service.settings.pipeline_key)
    restarted = Service(service.settings, service.immich, service.detector, service.recognizer)
    restarted.start()
    assert restarted.store.one("SELECT status FROM jobs")["status"] == "queued"
    restarted.close()


def test_human_decisions_survive_rematching(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    processed.decide(detection, None, "rejected")
    processed.rematch()
    assert processed.store.one("SELECT status FROM reviews")["status"] == "rejected"


def test_confirm_and_publish_once_with_auto_tag_disabled(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(detection, pet["id"], "confirmed")
    assert processed.publish_one()
    assert not processed.publish_one()
    assert processed.immich.tag_calls == [(processed.immich.items[0]["id"], "Pets/Mochi")]


def test_failed_publication_is_visible_and_retryable(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(detection, pet["id"], "confirmed")
    processed.immich.fail_tag = True
    processed.publish_one()
    assert processed.store.one("SELECT published,error FROM reviews") == {
        "published": 0,
        "error": "Test write failure",
    }
    assert not processed.publish_one()
    processed.immich.fail_tag = False
    processed.retry_failed()
    processed.publish_one()
    assert processed.store.one("SELECT published FROM reviews")["published"] == 1


def test_stale_or_hidden_assets_cannot_be_published(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.decide(detection, pet["id"], "confirmed")
    processed.immich.items[0]["visibility"] = "hidden"
    processed.publish_one()
    assert processed.immich.tag_calls == []
    assert processed.store.one("SELECT published FROM reviews")["published"] == 0


def test_photo_revision_invalidates_detection_cache(processed):
    processed.immich.items[0]["checksum"] = "changed"
    assert processed.sync() == 1
    processed.process_one()
    assert processed.detector.calls == 2
    assert processed.recognizer.calls == 2


def test_gallery_is_required_for_auto_match():
    result = match_pet(np.array([1.0, 0.0]), "dog", [], 0.85, 0.08, 10)
    assert asdict(result) == {
        "pet_id": None,
        "score": 0.0,
        "margin": 0.0,
        "references": 0,
        "confident": False,
    }


def test_hard_negative_blocks_lookalike():
    vector = np.array([1.0, 0.0])
    pet = {"id": "mochi", "species": "dog", "positive": [vector] * 10, "negative": [vector]}
    result = match_pet(vector, "dog", [pet], 0.85, 0.08, 10)
    assert result.pet_id == "mochi"
    assert result.margin == 0
    assert not result.confident


def test_close_rival_or_insufficient_references_blocks_auto_match():
    vector = np.array([1.0, 0.0])
    pet = {"id": "a", "species": "dog", "positive": [vector], "negative": []}
    assert not match_pet(vector, "dog", [pet], 0.85, 0.08, 10).confident
    pet["positive"] *= 10
    rival = {**pet, "id": "b"}
    assert not match_pet(vector, "dog", [pet, rival], 0.85, 0.08, 10).confident
    assert match_pet(vector, "dog", [pet], 0.85, 0.08, 10).confident
    assert not match_pet(vector, "cat", [pet], 0.85, 0.08, 10).confident


def test_human_correction_teaches_positive_and_negative_examples(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    first = processed.add_pet("Mochi", "dog")
    second = processed.add_pet("Milo", "dog")
    processed.decide(detection, first["id"], "confirmed", learn=True)
    processed.decide(detection, second["id"], "confirmed", learn=True)
    gallery = {pet["id"]: pet for pet in processed.references()}
    assert len(gallery[first["id"]]["positive"]) == 0
    assert len(gallery[first["id"]]["negative"]) == 1
    assert len(gallery[second["id"]]["positive"]) == 1
    processed.rematch()
    assert processed.store.one("SELECT pet_id,status FROM reviews") == {
        "pet_id": second["id"],
        "status": "confirmed",
    }


def test_unfinished_gallery_matching_is_recovered_on_restart(processed):
    pet = processed.add_pet("Mochi", "dog")
    detection = processed.store.one("SELECT id FROM detections")["id"]
    processed.decide(detection, pet["id"], "confirmed", learn=True)
    restarted = Service(
        processed.settings, processed.immich, processed.detector, processed.recognizer
    )
    assert restarted.suggestions_pending()
    restarted.rematch()
    assert not restarted.suggestions_pending()
    assert restarted.store.one("SELECT status FROM reviews")["status"] == "confirmed"
    restarted.close()


def test_untag_removes_assignment_and_example_without_deleting_photo(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Butter", "dog")
    processed.decide(detection, pet["id"], "confirmed", learn=True)
    processed.publish_one()
    processed.decide(detection, None, "rejected", expected_pet_id=pet["id"])
    assert processed.references()[0]["positive"] == []
    assert processed.publish_one()
    assert processed.immich.assigned_tags[processed.immich.items[0]["id"]] == set()
    assert len(processed.immich.items) == 1
    assert processed.store.one("SELECT status,published,error FROM reviews") == {
        "status": "rejected",
        "published": 0,
        "error": None,
    }
    assert not processed.publish_one()
    processed.rematch()
    assert processed.store.one("SELECT status FROM reviews")["status"] == "rejected"


def test_failed_tag_correction_survives_restart_and_retries(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    first = processed.add_pet("Butter", "dog")
    second = processed.add_pet("Rusty", "dog")
    processed.decide(detection, first["id"], "confirmed", learn=True)
    processed.publish_one()
    processed.decide(detection, second["id"], "confirmed", learn=True)
    processed.immich.fail_untag = True
    processed.publish_one()
    assert processed.store.one("SELECT published,error FROM reviews") == {
        "published": 0,
        "error": "Test tag removal failure",
    }
    assert processed.store.one("SELECT count(*) n FROM tag_removals")["n"] == 1
    restarted = Service(
        processed.settings, processed.immich, processed.detector, processed.recognizer
    )
    restarted.immich.fail_untag = False
    restarted.retry_failed()
    assert restarted.publish_one()
    assert restarted.immich.assigned_tags[restarted.immich.items[0]["id"]] == {"Pets/Rusty"}
    assert restarted.store.one("SELECT count(*) n FROM tag_removals")["n"] == 0
    assert restarted.store.one("SELECT published,error FROM reviews") == {
        "published": 1,
        "error": None,
    }
    restarted.close()


def test_tag_removal_keeps_a_tag_needed_by_another_animal_in_the_photo(processed):
    first_crop = processed.store.one("SELECT * FROM detections")
    second_crop = {**first_crop, "id": "b" * 32}
    processed.store.execute(
        "INSERT INTO detections VALUES "
        "(:id,:asset_id,:revision,:detector,:species,:score,:box,:crop)",
        second_crop,
    )
    processed.store.save_embedding(
        second_crop["id"],
        processed.settings.recognizer_key,
        processed.store.embedding(first_crop["id"], processed.settings.recognizer_key),
    )
    processed.evaluate(second_crop)
    butter = processed.add_pet("Butter", "dog")
    rusty = processed.add_pet("Rusty", "dog")
    for crop in [first_crop, second_crop]:
        processed.decide(crop["id"], butter["id"], "confirmed", learn=True)
        processed.publish_one()
    processed.decide(first_crop["id"], rusty["id"], "confirmed", learn=True)
    processed.publish_one()
    assert processed.immich.untag_calls == []
    assert processed.immich.assigned_tags[first_crop["asset_id"]] == {"Pets/Butter", "Pets/Rusty"}
    processed.decide(second_crop["id"], None, "rejected")
    processed.publish_one()
    assert processed.immich.assigned_tags[first_crop["asset_id"]] == {"Pets/Rusty"}


def test_reversing_a_partly_published_correction_keeps_only_final_identity(processed):
    detection = processed.store.one("SELECT id FROM detections")["id"]
    butter = processed.add_pet("Butter", "dog")
    rusty = processed.add_pet("Rusty", "dog")
    processed.decide(detection, butter["id"], "confirmed", learn=True)
    processed.publish_one()
    processed.decide(detection, rusty["id"], "confirmed", learn=True)
    processed.immich.fail_untag = True
    processed.publish_one()
    processed.decide(detection, butter["id"], "confirmed", learn=True)
    processed.immich.fail_untag = False
    processed.publish_one()
    assert processed.immich.assigned_tags[processed.immich.items[0]["id"]] == {"Pets/Butter"}
    assert processed.store.one("SELECT count(*) n FROM tag_removals")["n"] == 0


def test_automation_is_per_pet_and_never_teaches_itself(processed):
    from uuid import uuid4

    processed.settings.min_references = 1
    first = processed.store.one("SELECT * FROM detections")
    mochi = processed.add_pet("Mochi", "dog")
    milo = processed.add_pet("Milo", "dog")
    processed.add_reference(mochi["id"], first["id"], "positive")
    asset = {**processed.immich.items[0], "id": str(uuid4())}
    processed.immich.items.append(asset)
    processed.sync()
    processed.process_one()
    second = processed.store.one("SELECT * FROM detections WHERE asset_id=?", (asset["id"],))
    processed.store.save_embedding(
        second["id"], processed.settings.recognizer_key, np.array([0, 1, 0], dtype=np.float32)
    )
    processed.add_reference(milo["id"], second["id"], "positive")
    processed.set_automation(mochi["id"], True)
    processed.rematch()
    reviews = {r["detection_id"]: r for r in processed.store.rows("SELECT * FROM reviews")}
    assert reviews[first["id"]]["status"] == "confirmed"
    assert reviews[second["id"]]["status"] == "pending"
    assert processed.store.rows("SELECT detection_id FROM automatic_reviews") == [
        {"detection_id": first["id"]}
    ]
    assert processed.store.one("SELECT count(*) n FROM reference_images")["n"] == 2
    assert processed.publish_one()
    assert processed.immich.tag_calls == [(first["asset_id"], "Pets/Mochi")]
    pet = next(p for p in processed.pets() if p["id"] == mochi["id"])
    assert pet["auto_tagged_count"] == 1
    assert pet["tagged_count"] == 1


@pytest.mark.parametrize("gate", ["examples", "similarity", "margin"])
def test_per_pet_automation_respects_every_gate(processed, gate):
    detection = processed.store.one("SELECT * FROM detections")
    pet = processed.add_pet("Mochi", "dog")
    processed.add_reference(pet["id"], detection["id"], "positive")
    processed.settings.min_references = 2 if gate == "examples" else 1
    if gate == "similarity":
        processed.store.save_embedding(
            detection["id"],
            processed.settings.recognizer_key,
            np.array([0.6, 0.8, 0], dtype=np.float32),
        )
        # A separate held-out vector avoids comparing the reference with itself.
        processed.references = lambda: [{**pet, "positive": [np.array([1, 0, 0])], "negative": []}]
    if gate == "margin":
        rival = processed.add_pet("Milo", "dog")
        processed.add_reference(rival["id"], detection["id"], "positive")
    processed.set_automation(pet["id"], True)
    processed.rematch()
    assert processed.store.one("SELECT status FROM reviews")["status"] == "pending"
    assert not processed.publish_one()


def test_pause_cancels_only_unpublished_automatic_work_and_survives_restart(processed):
    processed.settings.min_references = 1
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.add_reference(pet["id"], detection, "positive")
    processed.set_automation(pet["id"], True)
    processed.rematch()
    assert processed.store.one("SELECT status FROM reviews")["status"] == "confirmed"
    processed.set_automation(pet["id"], False)
    assert processed.store.one("SELECT status FROM reviews")["status"] == "pending"
    assert not processed.publish_one()
    processed.settings.auto_tag = True  # A saved choice overrides the deployment default.
    restarted = Service(
        processed.settings, processed.immich, processed.detector, processed.recognizer
    )
    assert not restarted.automation_enabled(pet["id"])
    assert restarted.suggestions_pending()
    restarted.rematch()
    assert restarted.store.one("SELECT status FROM reviews")["status"] == "pending"
    restarted.decide(detection, pet["id"], "confirmed")
    restarted.set_automation(pet["id"], False)
    assert restarted.publish_one()  # Human confirmation is not cancelled by pause.
    assert restarted.store.rows("SELECT * FROM automatic_reviews") == []


def test_pause_keeps_published_automatic_tags_and_human_correction_clears_source(processed):
    processed.settings.min_references = 1
    detection = processed.store.one("SELECT id FROM detections")["id"]
    pet = processed.add_pet("Mochi", "dog")
    processed.add_reference(pet["id"], detection, "positive")
    processed.set_automation(pet["id"], True)
    processed.rematch()
    processed.publish_one()
    processed.set_automation(pet["id"], False)
    assert processed.store.one("SELECT status,published FROM reviews") == {
        "status": "confirmed",
        "published": 1,
    }
    assert processed.pets()[0]["auto_tagged_count"] == 1
    processed.decide(detection, pet["id"], "confirmed", learn=True)
    assert processed.store.rows("SELECT * FROM automatic_reviews") == []
    assert processed.pets()[0]["auto_tagged_count"] == 0
    assert processed.pets()[0]["positive_count"] == 1


def test_corrected_species_survives_restart_rematch_and_repeated_detector_output(processed):
    from immich_pet_recognition.models import Detection

    crop = processed.store.one("SELECT * FROM detections")
    processed.store.execute("UPDATE detections SET species='cat'")
    processed.detector.items = [Detection("cat", 0.95, (10, 10, 80, 70))]
    wang = processed.add_pet("Wang Wang", "dog")
    processed.decide(crop["id"], wang["id"], "confirmed", learn=True)
    processed.publish_one()
    restarted = Service(
        processed.settings, processed.immich, processed.detector, processed.recognizer
    )
    restarted.rematch()
    restarted.store.execute("DELETE FROM passes")
    restarted.store.execute("UPDATE jobs SET status='queued'")
    assert restarted.process_one()
    assert restarted.store.one("SELECT species FROM detections")["species"] == "dog"
    assert restarted.store.one("SELECT pet_id,status,published FROM reviews") == {
        "pet_id": wang["id"],
        "status": "confirmed",
        "published": 1,
    }
    assert restarted.pets()[0]["positive_count"] == 1
    assert not restarted.publish_one()


def test_cross_species_correction_moves_published_tags_without_invalid_examples(processed):
    crop = processed.store.one("SELECT * FROM detections")
    processed.store.execute("UPDATE detections SET species='cat'")
    cat = processed.add_pet("Cat example", "cat")
    wang = processed.add_pet("Wang Wang", "dog")
    processed.decide(crop["id"], cat["id"], "confirmed", learn=True)
    processed.publish_one()
    processed.decide(crop["id"], wang["id"], "confirmed", learn=True)
    processed.publish_one()
    assert processed.immich.assigned_tags[crop["asset_id"]] == {"Pets/Wang Wang"}
    pets = {pet["name"]: pet for pet in processed.pets()}
    assert pets["Wang Wang"]["positive_count"] == 1
    assert pets["Cat example"]["positive_count"] == pets["Cat example"]["negative_count"] == 0
    assert processed.store.rows("SELECT * FROM tag_removals") == []
