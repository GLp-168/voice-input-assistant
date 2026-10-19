"""Lightweight speaker clustering backend for the frozen Windows runtime."""

from __future__ import annotations

import numpy as np
import torch


def _normalized_rows(values: object) -> np.ndarray:
    if torch.is_tensor(values):
        values = values.detach().cpu().numpy()
    array = np.asarray(values, dtype=np.float32)
    if array.ndim != 2:
        raise ValueError("speaker embeddings must be a two-dimensional array")
    if array.shape[0] == 0:
        return array
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    return array / np.maximum(norms, np.finfo(np.float32).eps)


def _centroids(values: np.ndarray, labels: np.ndarray) -> np.ndarray:
    centers = []
    for label in range(int(labels.max()) + 1):
        center = values[labels == label].mean(axis=0)
        norm = max(float(np.linalg.norm(center)), float(np.finfo(np.float32).eps))
        centers.append(center / norm)
    return np.asarray(centers, dtype=np.float32)


def _renumber(labels: np.ndarray) -> np.ndarray:
    mapping: dict[int, int] = {}
    return np.asarray(
        [mapping.setdefault(int(label), len(mapping)) for label in labels],
        dtype=np.int64,
    )


def _threshold_cluster(values: np.ndarray, threshold: float) -> np.ndarray:
    labels: list[int] = []
    centers: list[np.ndarray] = []
    counts: list[int] = []
    for embedding in values:
        if not centers:
            labels.append(0)
            centers.append(embedding.copy())
            counts.append(1)
            continue
        similarities = np.asarray(centers) @ embedding
        label = int(np.argmax(similarities))
        if float(similarities[label]) < threshold:
            label = len(centers)
            centers.append(embedding.copy())
            counts.append(1)
        else:
            counts[label] += 1
            center = centers[label] + (embedding - centers[label]) / counts[label]
            centers[label] = center / max(
                float(np.linalg.norm(center)),
                float(np.finfo(np.float32).eps),
            )
        labels.append(label)

    result = np.asarray(labels, dtype=np.int64)
    while result.size and int(result.max()) > 0:
        centers_array = _centroids(values, result)
        similarities = centers_array @ centers_array.T
        np.fill_diagonal(similarities, -1.0)
        left, right = np.unravel_index(int(np.argmax(similarities)), similarities.shape)
        if float(similarities[left, right]) < threshold:
            break
        result[result == right] = left
        result = _renumber(result)
    return result


def _cosine_kmeans(values: np.ndarray, cluster_count: int) -> np.ndarray:
    cluster_count = max(1, min(int(cluster_count), values.shape[0]))
    centers = [values[0]]
    while len(centers) < cluster_count:
        similarities = values @ np.asarray(centers).T
        next_index = int(np.argmin(np.max(similarities, axis=1)))
        centers.append(values[next_index])
    centers_array = np.asarray(centers, dtype=np.float32)
    labels = np.full(values.shape[0], -1, dtype=np.int64)
    for _ in range(30):
        next_labels = np.argmax(values @ centers_array.T, axis=1).astype(np.int64)
        if np.array_equal(next_labels, labels):
            break
        labels = next_labels
        for label in range(cluster_count):
            members = values[labels == label]
            if members.size == 0:
                continue
            center = members.mean(axis=0)
            centers_array[label] = center / max(
                float(np.linalg.norm(center)),
                float(np.finfo(np.float32).eps),
            )
    return _renumber(labels)


class ClusterBackend(torch.nn.Module):
    """Cluster speaker embeddings without SciPy, sklearn, UMAP, or numba."""

    def __init__(self, merge_thr: float = 0.78) -> None:
        super().__init__()
        self.model_config = {"merge_thr": float(merge_thr)}

    def forward(self, embeddings: object, **params: object) -> np.ndarray:
        values = _normalized_rows(embeddings)
        if values.shape[0] < 2:
            return np.zeros(values.shape[0], dtype=np.int64)
        oracle_num = params.get("oracle_num")
        if oracle_num is not None:
            return _cosine_kmeans(values, int(oracle_num))
        return _threshold_cluster(values, self.model_config["merge_thr"])
