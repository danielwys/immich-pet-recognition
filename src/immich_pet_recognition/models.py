from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from huggingface_hub import hf_hub_download
from PIL import Image

from .config import Settings


@dataclass(frozen=True)
class Detection:
    species: str
    score: float
    box: tuple[int, int, int, int]


class Detector(Protocol):
    def detect(self, image: Image.Image) -> list[Detection]: ...


class Recognizer(Protocol):
    def embed(self, image: Image.Image) -> np.ndarray: ...


def preprocess(image: Image.Image, size: int) -> np.ndarray:
    image = image.convert("RGB").resize((size, size), Image.Resampling.BILINEAR)
    values = np.asarray(image, dtype=np.float32) / 255.0
    values = (values - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
        [0.229, 0.224, 0.225], dtype=np.float32
    )
    return np.ascontiguousarray(values.transpose(2, 0, 1)[None], dtype=np.float32)


def normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or not np.isfinite(norm) or norm < 1e-8:
        raise ValueError("Model returned an invalid embedding")
    return vector / norm


def session(repo: str, revision: str, filename: str, cache: Path, provider: str):
    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError("Install the cpu or cuda extra to run real inference") from None
    if provider not in ort.get_available_providers():
        raise RuntimeError(f"{provider} unavailable; check the runtime and NVIDIA container setup")
    path = hf_hub_download(repo, filename, revision=revision, cache_dir=cache)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    result = ort.InferenceSession(path, sess_options=options, providers=[provider])
    if result.get_providers()[0] != provider:
        raise RuntimeError(f"Requested {provider} did not initialize; refusing silent CPU fallback")
    return result


def decode_detections(boxes, logits, size: tuple[int, int], threshold: float) -> list[Detection]:
    # COCO's original 91-class IDs; competing animal classes prevent horse -> dog forcing.
    animal_ids = np.array([16, 17, 18, 19, 20, 21])
    values = 1 / (1 + np.exp(-np.clip(logits[0][:, animal_ids], -80, 80)))
    winners = values.argmax(axis=1)
    width, height = size
    detections = []
    for index, winner in enumerate(winners):
        category = animal_ids[winner]
        score = float(values[index, winner])
        if category not in (17, 18) or score < threshold:
            continue
        cx, cy, w, h = boxes[0][index]
        x1 = max(0, min(width, int(np.floor((cx - w / 2) * width))))
        y1 = max(0, min(height, int(np.floor((cy - h / 2) * height))))
        x2 = max(0, min(width, int(np.ceil((cx + w / 2) * width))))
        y2 = max(0, min(height, int(np.ceil((cy + h / 2) * height))))
        if x2 > x1 and y2 > y1:
            detections.append(
                Detection("cat" if category == 17 else "dog", score, (x1, y1, x2, y2))
            )
    return sorted(detections, key=lambda d: (d.box, d.species))


class RFDetrDetector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runtime = None

    def detect(self, image: Image.Image) -> list[Detection]:
        if self.runtime is None:
            self.runtime = session(
                self.settings.detector_repo,
                self.settings.detector_revision,
                "detection/model.onnx",
                self.settings.data_dir / "models",
                self.settings.provider,
            )
        boxes, logits = self.runtime.run(["dets", "labels"], {"input": preprocess(image, 384)})
        return decode_detections(boxes, logits, image.size, self.settings.detector_threshold)


class NoodleRecognizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runtime = None

    def embed(self, image: Image.Image) -> np.ndarray:
        if self.runtime is None:
            self.runtime = session(
                self.settings.recognizer_repo,
                self.settings.recognizer_revision,
                "recognition/model.onnx",
                self.settings.data_dir / "models",
                self.settings.provider,
            )
        output = self.runtime.run(["embedding"], {"input": preprocess(image, 224)})[0][0]
        if output.shape != (512,):
            raise ValueError("Expected a 512-dimensional Open Noodle embedding")
        return normalize(output)
