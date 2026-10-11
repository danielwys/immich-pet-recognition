import hashlib
import json

import numpy as np
from PIL import Image, ImageDraw


def seed_demo(service):
    """Synthetic crop illustrations and vectors. No real photo recognition or external requests."""
    if service.store.state("demo_seeded"):
        return
    pet = service.add_pet("Your Westie", "dog")
    colors = ["#d7dfca", "#d3dde7", "#e3d3c1", "#d8cedd", "#d3d9cf", "#e4dacc"]
    names = [
        "Garden afternoon",
        "Weekend walk",
        "A new face",
        "Sofa companion",
        "At the park",
        "Quiet morning",
    ]
    for index, (color, name) in enumerate(zip(colors, names, strict=True)):
        detection_id = hashlib.md5(f"demo-{index}".encode(), usedforsecurity=False).hexdigest()
        asset_id = f"demo-asset-{index}"
        revision = "demo"
        service.store.execute("INSERT INTO assets VALUES (?,?,?)", (asset_id, revision, name))
        canvas = Image.new("RGB", (600, 400), color)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((185, 285, 415, 335), fill="#a8af9d")
        draw.polygon([(210, 130), (210, 45), (265, 110)], fill="#f7f4eb")
        draw.polygon([(335, 110), (390, 45), (390, 130)], fill="#f7f4eb")
        draw.ellipse((190, 90, 410, 300), fill="#f7f4eb")
        draw.ellipse((240, 172, 253, 187), fill="#343f36")
        draw.ellipse((347, 172, 360, 187), fill="#343f36")
        draw.ellipse((283, 211, 317, 236), fill="#343f36")
        draw.line([(300, 236), (300, 250), (285, 259)], fill="#343f36", width=3)
        draw.line([(300, 250), (315, 259)], fill="#343f36", width=3)
        draw.text((20, 370), "DEMO ILLUSTRATION - NOT A REAL PHOTO", fill="#435046")
        crop = detection_id + ".jpg"
        canvas.save(service.crops / crop)
        service.store.execute(
            "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?)",
            (
                detection_id,
                asset_id,
                revision,
                service.settings.detector_key,
                "dog",
                0.95,
                json.dumps([0, 0, 600, 400]),
                crop,
            ),
        )
        vector = np.zeros(512, dtype=np.float32)
        vector[0], vector[1] = 0.95 - index * 0.07, 0.3 + index * 0.05
        vector /= np.linalg.norm(vector)
        service.store.save_embedding(detection_id, service.settings.recognizer_key, vector)
        service.store.execute(
            "INSERT INTO reviews(detection_id,model,pet_id,score,margin) VALUES (?,?,?,?,?)",
            (detection_id, service.settings.recognizer_key, pet["id"], 0.94 - index * 0.07, 0.12),
        )
    service.store.set_state("demo_seeded", "true")
