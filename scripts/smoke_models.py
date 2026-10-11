"""Run the real pinned models on a local image, without connecting to Immich."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from immich_pet_recognition.config import Settings
from immich_pet_recognition.models import NoodleRecognizer, RFDetrDetector

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("image", type=Path)
parser.add_argument("--cache", type=Path, default=Path("data/smoke"))
parser.add_argument("--cuda", action="store_true")
args = parser.parse_args()
settings = Settings(
    _env_file=None,
    service_token="local-smoke-only",
    background=False,
    data_dir=args.cache,
    provider="CUDAExecutionProvider" if args.cuda else "CPUExecutionProvider",
)
with Image.open(args.image) as source:
    image = ImageOps.exif_transpose(source).convert("RGB")
started = time.monotonic()
detector = RFDetrDetector(settings)
detections = detector.detect(image)
detector_seconds = time.monotonic() - started
assert detections, "No dogs/cats above the configured detector threshold in this image"
recognizer = NoodleRecognizer(settings)
started = time.monotonic()
vectors = [recognizer.embed(image.crop(d.box)) for d in detections]
recognition_seconds = time.monotonic() - started
assert all(v.shape == (512,) and np.isfinite(v).all() for v in vectors)
assert all(abs(float(np.linalg.norm(v)) - 1) < 1e-5 for v in vectors)
print(
    json.dumps(
        {
            "provider": settings.provider,
            "detector": settings.detector_key,
            "recognizer": settings.recognizer_key,
            "detections": [
                {"species": d.species, "confidence": d.score, "box": d.box} for d in detections
            ],
            "embedding_dimensions": [int(v.size) for v in vectors],
            "embedding_norms": [float(np.linalg.norm(v)) for v in vectors],
            "detector_seconds_including_load": detector_seconds,
            "recognition_seconds_including_load": recognition_seconds,
        },
        indent=2,
    )
)
