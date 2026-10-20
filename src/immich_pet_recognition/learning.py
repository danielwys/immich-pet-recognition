"""Fit decision boundaries to explicit feedback, leaving each source photo out.

This updates the identity decision policy, not the pretrained image encoder. The
checks below describe performance on past feedback, not calibrated probabilities.
"""

import numpy as np

PROFILES = {"careful": 1.0, "balanced": 0.98, "broader": 0.95}


def _score(vector, examples):
    if not examples:
        return -1.0
    values = np.asarray(examples, dtype=np.float32) @ vector
    count = min(3, len(values))
    return float(np.mean(np.partition(values, len(values) - count)[-count:]))


def fit_policies(gallery, threshold, margin, minimum, profile="balanced"):
    """One rectangle per pet, fitted using only human-enrolled references.

    All crops of the query photo are removed from every pet's positive AND
    negative gallery. Automatic assignments never become calibration labels.
    Explicit 'not this pet' feedback supplies contrasts.
    """
    records = {}
    for pet in gallery:
        for kind in ("positive", "negative"):
            for index, vector in enumerate(pet[kind]):
                ids = pet.get(kind + "_ids", [])
                assets = pet.get(kind + "_assets", [])
                if index >= len(ids) or index >= len(assets):
                    continue
                record = records.setdefault(
                    ids[index],
                    {
                        "vector": vector,
                        "asset": assets[index],
                        "species": pet["species"],
                        "positive": set(),
                        "negative": set(),
                    },
                )
                record[kind].add(pet["id"])
    samples = {pet["id"]: [] for pet in gallery}
    for record in records.values():
        # Ambiguous historical examples must not teach either identity.
        if len(record["positive"]) > 1:
            continue
        scores, negatives = {}, {}
        for pet in gallery:
            if pet["species"] != record["species"]:
                continue
            vectors = {}
            for kind in ("positive", "negative"):
                assets = pet.get(kind + "_assets", [])
                vectors[kind] = [
                    vector
                    for index, vector in enumerate(pet[kind])
                    if index < len(assets) and assets[index] != record["asset"]
                ]
            scores[pet["id"]] = _score(record["vector"], vectors["positive"])
            negatives[pet["id"]] = max(
                (float(record["vector"] @ v) for v in vectors["negative"]), default=-1.0
            )
        for pet_id, score in scores.items():
            if pet_id in record["positive"]:
                correct = True
            elif record["positive"] or pet_id in record["negative"]:
                correct = False
            else:
                continue  # An unlabeled pet is not a known negative for every identity.
            rival = max((v for key, v in scores.items() if key != pet_id), default=-1.0)
            gap = score - max(rival, negatives[pet_id])
            samples[pet_id].append((score, gap, correct, record["asset"]))
    policies = {}
    for pet in gallery:
        rows = samples[pet["id"]]
        positive = {row[3] for row in rows if row[2]}
        other = {row[3] for row in rows if not row[2]}
        policy = {
            "threshold": min(threshold, 0.75) if profile != "careful" else threshold,
            "margin": min(margin, 0.03) if profile != "careful" else margin,
            "min_references": min(minimum, 3),
            "state": "starting",
            "positive_checked": len(positive),
            "other_checked": len(other),
            "accepted_correct": 0,
            "accepted_incorrect": 0,
        }
        if len(positive) >= 5 and len(other) >= 3:
            scores, gaps, truth = np.array([row[:3] for row in rows]).T
            truth = truth.astype(bool)
            positive_scores = scores[truth & (gaps > 0)]
            quantile, allowance = {
                "careful": (0.1, 0),
                "balanced": (0.05, 0.025),
                "broader": (0.01, 0.05),
            }[profile]
            floor = 1.01
            if len(positive_scores):
                positive_edge = float(np.quantile(positive_scores, quantile))
                negative_edge = float(np.quantile(scores[~truth], 0.99))
                # When identities are well separated, put the boundary in the
                # observed gap instead of demanding near-duplicate similarity.
                boundary = (positive_edge + negative_edge) / 2
                if profile == "broader":
                    boundary = positive_edge * 0.3 + negative_edge * 0.7
                floor = (
                    max(0.45, min(positive_edge - allowance, boundary))
                    if (profile != "careful")
                    else max(0.45, positive_edge)
                )
            # Candidate boundaries include the observations on both sides of the
            # decision, with a bounded search for predictable gallery refresh time.
            score_values = np.unique(np.r_[floor, np.quantile(scores, np.linspace(0, 1, 65))])
            gap_floor = {"careful": 0.05, "balanced": 0.02, "broader": 0.005}[profile]
            gap_values = np.unique(np.r_[gap_floor, np.quantile(gaps, np.linspace(0, 1, 33))])
            best = None
            for cutoff in score_values[score_values >= floor]:
                for separation in gap_values[gap_values >= gap_floor]:
                    accepted = (scores >= cutoff) & (gaps >= separation)
                    correct = int(np.sum(accepted & truth))
                    wrong = int(np.sum(accepted & ~truth))
                    if correct < 3 or correct / (correct + wrong) < PROFILES[profile]:
                        continue
                    rank = (correct, -wrong, -float(cutoff + separation))
                    if best is None or rank > best[0]:
                        best = (rank, float(cutoff), float(separation), correct, wrong)
            if best:
                _, cutoff, separation, correct, wrong = best
                policy.update(
                    threshold=cutoff,
                    margin=separation,
                    state="fitted",
                    accepted_correct=correct,
                    accepted_incorrect=wrong,
                )
            else:
                # Contradictory labels should cause a useful group question,
                # rather than forcing an unearned automatic identity.
                policy.update(threshold=1.01, state="overlap")
        policies[pet["id"]] = policy
    return policies
