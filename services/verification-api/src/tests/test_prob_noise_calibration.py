# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


THIS_FILE = Path(__file__).resolve()
SRC_DIR = THIS_FILE.parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from proof_verifier import _prob_noise_bucket_decision  # noqa: E402


NOISE_QT = [
    (0.005, 0.50),
    (0.01, 0.1),
    (0.03, 0.01),
    (0.05, 2 / 256),
    (0.15, 1 / 256),
]


def test_prob_noise_uses_legacy_bucket_without_candidate_null():
    noise = np.zeros(256, dtype=np.float64)
    noise[:3] = 0.04

    result = _prob_noise_bucket_decision(
        noise,
        NOISE_QT,
        adaptive_enabled=True,
        loo_slack=0,
    )

    assert result["adaptive_used"] is False
    assert result["old_allowed"][2] == 2
    assert result["final_allowed"][2] == 2
    assert result["obs_counts"][2] == 3
    assert result["valid"] is False


def test_prob_noise_loo_null_can_loosen_bucket_count():
    noise = np.zeros(256, dtype=np.float64)
    noise[:5] = 0.04

    candidate_noise = np.zeros((3, 256), dtype=np.float64)
    candidate_noise[1, :5] = 0.04
    candidate_noise[2, :5] = 0.08

    result = _prob_noise_bucket_decision(
        noise,
        NOISE_QT,
        candidate_sampling_noise=candidate_noise,
        adaptive_enabled=True,
        loo_slack=0,
        loo_quantile=1.0,
    )

    assert result["adaptive_used"] is True
    assert result["old_allowed"][2] == 2
    assert result["empirical_allowed"][2] == 5
    assert result["final_allowed"][2] == 5
    assert result["obs_counts"][2] == 5
    assert result["valid"] is True


def test_prob_noise_candidate_null_is_shadow_only_by_default(monkeypatch):
    monkeypatch.delenv("POW_PROB_NOISE_ADAPTIVE", raising=False)
    monkeypatch.delenv("POW_PROB_NOISE_ADAPTIVE_ENFORCE", raising=False)

    noise = np.zeros(256, dtype=np.float64)
    noise[:5] = 0.04

    candidate_noise = np.zeros((3, 256), dtype=np.float64)
    candidate_noise[1, :5] = 0.04
    candidate_noise[2, :5] = 0.08

    result = _prob_noise_bucket_decision(
        noise,
        NOISE_QT,
        candidate_sampling_noise=candidate_noise,
        loo_slack=0,
        loo_quantile=1.0,
    )

    assert result["adaptive_available"] is True
    assert result["adaptive_enforced"] is False
    assert result["adaptive_valid"] is True
    assert result["valid"] is False
    assert result["final_allowed"][2] == result["old_allowed"][2] == 2
    assert result["adaptive_allowed"][2] == 5


def test_prob_noise_loo_null_is_never_tighter_than_legacy_floor():
    noise = np.zeros(256, dtype=np.float64)
    noise[:2] = 0.04

    candidate_noise = np.zeros((3, 256), dtype=np.float64)

    result = _prob_noise_bucket_decision(
        noise,
        NOISE_QT,
        candidate_sampling_noise=candidate_noise,
        adaptive_enabled=True,
        loo_slack=0,
        loo_quantile=1.0,
    )

    assert result["adaptive_used"] is True
    assert result["empirical_allowed"][2] == 0
    assert result["final_allowed"][2] == result["old_allowed"][2] == 2
    assert result["valid"] is True
