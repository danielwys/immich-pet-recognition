from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from immich_pet_recognition.api import create_app
from immich_pet_recognition.service import Service

AUTH = {"Authorization": "Bearer test-token"}


def three_photos(service):
    original = service.immich.items[0]
    service.immich.items.extend({**original, "id": str(uuid4())} for _ in range(2))
    service.sync()
    while service.process_one():
        pass


def restart(service):
    return Service(service.settings, service.immich, service.detector, service.recognizer)


def test_progress_is_readable_during_inference_and_finishes_only_after_all_crops(service):
    three_photos(service)
    service.gallery_changed()
    assert service.recheck_status()["state"] == "queued"
    assert service.recheck_status()["percent"] is None
    entered, release = Event(), Event()
    original = service.evaluate
    calls = []

    def evaluate(detection, gallery):
        calls.append(service.recheck_status())
        if len(calls) == 2:
            entered.set()
            assert release.wait(5)
        return original(detection, gallery)

    service.evaluate = evaluate
    with TestClient(create_app(service.settings, service), headers=AUTH) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(service.rematch)
            try:
                assert entered.wait(5)
                # Status must not wait for the GPU/model lock held by evaluate.
                progress = pool.submit(lambda: client.get("/api/status").json()).result(3)
                assert progress["recheck"]["state"] == "running"
                assert progress["recheck"]["completed"] == 1
                assert progress["recheck"]["total"] == 3
                assert progress["recheck"]["percent"] == 33
                assert progress["suggestions_updating"] is True
            finally:
                release.set()
            future.result(5)
        result = client.get("/api/status").json()
        assert result["recheck"]["state"] == "complete"
        assert result["recheck"]["percent"] == 100
        assert result["recheck"]["completed"] == result["recheck"]["total"] == 3
        assert result["suggestions_updating"] is False
        assert [call["percent"] for call in calls] == [0, 33, 66]
        assert restart(service).recheck_status() == service.recheck_status()
        response = client.post("/api/rematch")
        assert response.status_code == 202
        queued = service.recheck_status()
        assert queued["state"] == "queued" and queued["percent"] is None
        assert queued["completed"] == 0


def test_new_examples_interrupt_the_old_pass_without_false_completion(service):
    three_photos(service)
    service.gallery_changed()
    original = service.evaluate
    calls = 0

    def evaluate(detection, gallery):
        nonlocal calls
        calls += 1
        result = original(detection, gallery)
        if calls == 2:
            service.gallery_changed()
        return result

    service.evaluate = evaluate
    service.rematch()
    assert calls == 2
    assert service.recheck_status()["state"] == "queued"
    assert service.recheck_status()["completed"] == 0
    assert service.suggestions_pending()
    assert service.store.state("last_recheck") is None
    service.rematch()
    assert calls == 5
    assert service.recheck_status()["percent"] == 100
    assert not service.suggestions_pending()


def test_failed_recheck_reports_partial_work_and_retry_queues_a_fresh_pass(service):
    three_photos(service)
    service.gallery_changed()
    original = service.evaluate
    calls = 0

    def evaluate(detection, gallery):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Test inference failure")
        return original(detection, gallery)

    service.evaluate = evaluate
    with pytest.raises(RuntimeError, match="Test inference failure"):
        service.rematch()
    progress = service.recheck_status()
    assert progress["state"] == "failed"
    assert progress["completed"] == 1 and progress["percent"] == 33
    assert progress["error"] == "Test inference failure"
    assert service.suggestions_pending()
    assert restart(service).recheck_status()["state"] == "queued"
    service.retry_failed()
    assert service.recheck_status()["state"] == "queued"
    service.evaluate = original
    service.rematch()
    assert service.recheck_status()["state"] == "complete"
    assert service.recheck_status()["error"] == ""


def test_stop_and_restart_resume_instead_of_claiming_completion(processed):
    processed.gallery_changed()
    processed.stop_event.set()
    processed.rematch()
    assert processed.recheck_status()["state"] == "queued"
    assert processed.recheck_status()["percent"] is None
    restarted = restart(processed)
    assert restarted.suggestions_pending()
    restarted.rematch()
    assert restarted.recheck_status()["percent"] == 100


def test_empty_pass_is_complete_but_an_old_model_checkpoint_is_not_reused(service):
    service.gallery_changed()
    service.rematch()
    progress = service.recheck_status()
    assert progress["state"] == "complete"
    assert progress["completed"] == progress["total"] == 0
    assert progress["percent"] == 100
    service.settings.recognizer_revision = "new-model"
    progress = restart(service).recheck_status()
    assert progress["state"] == "idle" and progress["percent"] is None


def test_progress_counts_automatic_matches_without_learning_from_them(processed):
    processed.settings.min_references = 1
    pet = processed.add_pet("Rusty", "dog")
    detection = processed.store.one("SELECT id FROM detections")["id"]
    processed.add_reference(pet["id"], detection, "positive")
    processed.set_automation(pet["id"], True)
    processed.rematch()
    assert processed.recheck_status()["automatic_matches"] == 1
    assert processed.recheck_status()["completed"] == 1
    assert processed.pets()[0]["positive_count"] == 1
    assert processed.review_summary()["pending_tag_changes"] == 1
