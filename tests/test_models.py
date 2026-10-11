import numpy as np
import pytest
from PIL import Image

from immich_pet_recognition.models import decode_detections, normalize, preprocess


def test_rgb_square_imagenet_preprocessing():
    tensor = preprocess(Image.new("RGB", (80, 40), (255, 0, 0)), 224)
    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.dtype == np.float32
    assert tensor[0, :, 0, 0] == pytest.approx(
        [(1 - 0.485) / 0.229, -0.456 / 0.224, -0.406 / 0.225]
    )


def test_coco_91_classes_box_rescaling_and_other_animal_rejection():
    boxes = np.array([[[0.5, 0.5, 0.6, 0.5], [0.5, 0.5, 1.0, 1.0]]])
    logits = np.full((1, 2, 91), -10.0)
    logits[0, 0, 18] = 5.0  # dog
    logits[0, 1, 18] = 4.0
    logits[0, 1, 19] = 6.0  # horse beats dog; don't force it into a pet class
    found = decode_detections(boxes, logits, (100, 80), 0.5)
    assert len(found) == 1
    assert found[0].species == "dog"
    assert found[0].box == (20, 20, 80, 60)


@pytest.mark.parametrize("vector", [[0.0, 0.0], [float("nan"), 1.0]])
def test_invalid_embeddings_rejected(vector):
    with pytest.raises(ValueError):
        normalize(np.array(vector))
