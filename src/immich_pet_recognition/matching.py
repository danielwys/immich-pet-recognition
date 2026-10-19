from dataclasses import dataclass

import numpy as np


@dataclass
class Match:
    pet_id: str | None
    score: float
    margin: float
    references: int
    confident: bool


def match_pet(
    vector, species, references, threshold, margin, min_references, policies=None
) -> Match:
    """Compare top-three positive mean to rivals/negatives. Scores aren't probabilities."""
    candidates = []
    for pet in references:
        if pet["species"] != species or not pet["positive"]:
            continue
        similarities = sorted((float(vector @ v) for v in pet["positive"]), reverse=True)
        score = float(np.mean(similarities[:3]))
        negative = max((float(vector @ v) for v in pet["negative"]), default=-1.0)
        candidates.append((score, pet["id"], negative, len(similarities)))
    if not candidates:
        return Match(None, 0.0, 0.0, 0, False)
    candidates.sort(reverse=True)
    score, pet_id, negative, count = candidates[0]
    rival = candidates[1][0] if len(candidates) > 1 else -1.0
    gap = score - max(rival, negative)
    if policies and pet_id in policies:
        policy = policies[pet_id]
        threshold, margin, min_references = (
            policy["threshold"],
            policy["margin"],
            policy["min_references"],
        )
    confident = score >= threshold and gap >= margin and count >= min_references
    return Match(pet_id, score, gap, count, confident)
