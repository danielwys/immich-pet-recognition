import io
from uuid import uuid4

import numpy as np
import pytest
from PIL import Image

from immich_pet_recognition.config import Settings
from immich_pet_recognition.models import Detection
from immich_pet_recognition.service import Service


class FakeImmich:
    def __init__(self):
        self.items = [
            {
                "id": str(uuid4()),
                "checksum": "original",
                "fileModifiedAt": "2026-10-10",
                "thumbhash": "preview",
                "updatedAt": "2026-10-11",
                "type": "IMAGE",
                "originalFileName": "dog.jpg",
                "visibility": "timeline",
                "isTrashed": False,
            }
        ]
        self.tag_calls = []
        self.preview_calls = 0
        self.fail_tag = False

    def assets(self, since=None):
        yield from self.items

    def asset(self, asset_id):
        return next(a for a in self.items if a["id"] == asset_id)

    def preview(self, asset_id):
        self.preview_calls += 1
        buffer = io.BytesIO()
        Image.new("RGB", (100, 80), "white").save(buffer, format="JPEG")
        return buffer.getvalue()

    def tag(self, asset_id, name):
        if self.fail_tag:
            raise RuntimeError("Test write failure")
        self.tag_calls.append((asset_id, name))

    def close(self):
        pass


class FakeDetector:
    def __init__(self):
        self.calls = 0
        self.items = [Detection("dog", 0.95, (10, 10, 80, 70))]

    def detect(self, image):
        self.calls += 1
        return self.items


class FakeRecognizer:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def embed(self, image):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Test model failure")
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        service_token="test-token",
        immich_api_key="test-api-key",
        background=False,
    )


@pytest.fixture
def service(settings):
    return Service(settings, FakeImmich(), FakeDetector(), FakeRecognizer())


@pytest.fixture
def processed(service):
    assert service.sync() == 1
    assert service.process_one()
    return service
