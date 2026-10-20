"""Compact groups with a similarity bound between every pair of members."""

import hashlib

import numpy as np


def similar_groups(rows, similarity=0.8, maximum=200):
    buckets = {}
    for row in rows:
        buckets.setdefault((row["species"], row["pet_id"]), []).append(row)
    result = []
    for bucket in buckets.values():
        vectors = np.asarray([row["vector"] for row in bucket], dtype=np.float32)
        anchors = np.empty_like(vectors)
        clusters = []
        for index, vector in enumerate(vectors):
            target = None
            if clusters:
                scores = np.einsum("ij,j->i", anchors[: len(clusters)], vector)
                for candidate in np.argsort(-scores):
                    if scores[candidate] < similarity:
                        break
                    members = clusters[candidate]
                    if len(members) < maximum and np.all(
                        np.einsum("ij,j->i", vectors[members], vector) >= similarity - 1e-6
                    ):
                        target = int(candidate)
                        break
            if target is None:
                anchors[len(clusters)] = vector
                clusters.append([index])
            else:
                clusters[target].append(index)
        for indexes in clusters:
            members = [
                {k: v for k, v in bucket[index].items() if k != "vector"} for index in indexes
            ]
            ids = sorted(row["id"] for row in members)
            key = hashlib.sha256("|".join(ids).encode()).hexdigest()[:24]
            samples = sorted(set([0, len(members) // 3, 2 * len(members) // 3, len(members) - 1]))
            result.append(
                {
                    "id": key,
                    "pet_id": members[0]["pet_id"],
                    "species": members[0]["species"],
                    "members": members,
                    "crop_count": len(members),
                    "photo_count": len({row["asset_id"] for row in members}),
                    "asset_ids": sorted({row["asset_id"] for row in members}),
                    "samples": [members[index] for index in samples],
                }
            )
    return sorted(result, key=lambda group: (-group["photo_count"], group["id"]))
