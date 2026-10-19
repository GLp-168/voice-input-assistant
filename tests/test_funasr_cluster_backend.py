from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import torch


_BACKEND_PATH = (
    Path(__file__).resolve().parents[1]
    / "packaging"
    / "windows"
    / "funasr_patches"
    / "cluster_backend.py"
)
_SPEC = importlib.util.spec_from_file_location("funasr_cluster_backend", _BACKEND_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_BACKEND = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BACKEND)
ClusterBackend = _BACKEND.ClusterBackend


def _two_speaker_embeddings() -> np.ndarray:
    return np.asarray(
        [
            [1.00, 0.00, 0.02],
            [0.98, 0.04, 0.01],
            [0.99, -0.03, 0.00],
            [0.01, 1.00, 0.02],
            [-0.03, 0.98, 0.01],
            [0.02, 0.99, -0.02],
        ],
        dtype=np.float32,
    )


def test_threshold_clustering_separates_speakers_and_keeps_groups_together() -> None:
    labels = ClusterBackend(merge_thr=0.9)(_two_speaker_embeddings())

    assert labels.dtype == np.int64
    assert labels.shape == (6,)
    assert len(set(labels[:3])) == 1
    assert len(set(labels[3:])) == 1
    assert labels[0] != labels[3]


def test_oracle_num_uses_requested_cluster_count() -> None:
    backend = ClusterBackend(merge_thr=-1.0)

    threshold_labels = backend(_two_speaker_embeddings())
    oracle_labels = backend(_two_speaker_embeddings(), oracle_num=2)

    assert np.unique(threshold_labels).size == 1
    assert np.unique(oracle_labels).size == 2
    assert len(set(oracle_labels[:3])) == 1
    assert len(set(oracle_labels[3:])) == 1
    assert oracle_labels[0] != oracle_labels[3]


def test_accepts_torch_tensor_embeddings() -> None:
    embeddings = torch.tensor(_two_speaker_embeddings(), requires_grad=True)

    labels = ClusterBackend(merge_thr=0.9)(embeddings)

    assert isinstance(labels, np.ndarray)
    np.testing.assert_array_equal(labels[:3], np.repeat(labels[0], 3))
    np.testing.assert_array_equal(labels[3:], np.repeat(labels[3], 3))
    assert labels[0] != labels[3]
