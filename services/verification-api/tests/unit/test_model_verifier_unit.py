# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ModelVerifier difficulty validation and audit flow.

These tests are fast (<5s), require no GPU, and download no models.
ModelAuditor is monkeypatched throughout — only the difficulty math
and the validate() control flow are exercised.

Run via: ./tests/run_tests.sh unit
"""

import os
import json
import pytest
import time

# Ensure test mode is set so heavy imports are stubbed
os.environ.setdefault("TEST_MODE", "true")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_verifier(**env_overrides):
    """Create a ModelVerifier with controlled environment."""
    # Set env before importing (module-level reads)
    defaults = {
        "GENESIS_FLOPS_PER_TOKEN": "500000000",  # 500M
        "MODEL_DIFFICULTY_NORMALIZER": "1000000",
        "DIFFICULTY_TOLERANCE": "0.05",
        "GENESIS_BASELINES": "{}",
        "ACTIVE_CHAIN_GENESIS_MODEL": "",
    }
    defaults.update(env_overrides)
    for k, v in defaults.items():
        os.environ[k] = str(v)

    # Force re-import to pick up new env values
    import importlib
    import model_verifier as mv
    importlib.reload(mv)
    return mv.ModelVerifier(device="cpu"), mv


SAMPLE_REPORT = {
    "model_name": "test-model",
    "context_length": 128,
    "flops": {
        "total_flops": 1e11,
        "flops_per_token": 1e9,  # 1B FLOPs/token
        "active_ratio": 0.95,
    },
    "salient_weights": {"count": 500000, "total": 1000000, "percentage": 50.0},
    "validity": {
        "input_sensitivity_kl": 5.2,
        "single_token_kl": 0.3,
        "permutation_perplexity_ratio": 1.8,
    },
    "file_size_check": {
        "in_memory_bytes": 2000000000,
        "on_disk_bytes": None,
        "total_params": 500000000,
        "bits_per_weight": 32.0,
        "expected_bytes_from_salient": 2000000,
        "ratio_disk_to_expected": 1000.0,
    },
}


# ---------------------------------------------------------------------------
# TestDifficultyValidation — pure arithmetic, no GPU
# ---------------------------------------------------------------------------

class TestDifficultyValidation:
    """Test the inverse compute scalar difficulty formula.

    Formula: expected_difficulty = normalizer * genesis_fpt / model_fpt

    With genesis_fpt = 500M and normalizer = 1M:
      - model at 500M FLOPs → expected = 1M (identity)
      - model at 1B FLOPs (2x) → expected = 500K (halved — easier hash)
      - model at 250M FLOPs (0.5x) → expected = 2M (doubled — harder hash)
    """

    def test_identity_at_normalizer(self):
        """Model with same FLOPs as genesis → difficulty == normalizer."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_000_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]
        assert result["verdict"] == "pass"
        assert result["expected_difficulty"] == 1_000_000

    def test_double_flops_halves_difficulty(self):
        """2x FLOPs → expected difficulty = normalizer/2 = 500K (easier hash)."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=500_000,
            measured_fpt=1_000_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]
        assert result["verdict"] == "pass"
        assert result["expected_difficulty"] == 500_000

    def test_half_flops_doubles_difficulty(self):
        """0.5x FLOPs → expected difficulty = 2M (harder hash)."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=2_000_000,
            measured_fpt=250_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]
        assert result["verdict"] == "pass"
        assert result["expected_difficulty"] == 2_000_000

    def test_wrong_direction_fails(self):
        """2x FLOPs but claims 2M difficulty (should be 500K) → fail."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=2_000_000,
            measured_fpt=1_000_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert not result["within_tolerance"]
        assert result["verdict"] == "fail_difficulty_mismatch"
        # Ratio is 2M / 500K = 4.0 — way outside 5% tolerance
        assert result["ratio"] == pytest.approx(4.0, rel=1e-3)

    def test_boundary_gaming_rejected_at_5pct(self):
        """6% deviation from expected → rejected at 5% tolerance."""
        verifier, mv = _make_verifier()
        # Expected = 1M, claimed = 1.06M (6% over)
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_060_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert not result["within_tolerance"]
        assert result["verdict"] == "fail_difficulty_mismatch"

    def test_within_5pct_passes(self):
        """4% deviation → passes within 5% tolerance."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_040_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]
        assert result["verdict"] == "pass"

    def test_exactly_at_5pct_boundary(self):
        """Exactly 5% deviation → passes (tolerance is <=)."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_050_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]

    def test_negative_5pct_boundary(self):
        """5% under → passes."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=950_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]

    def test_genesis_not_configured_skips(self):
        """Missing genesis baseline → verdict 'skip', no crash."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_000_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=0.0,
        )
        assert result["verdict"] == "skip"
        assert "genesis baseline not configured" in result["reason"]

    def test_zero_measured_flops_fails(self):
        """Model with zero FLOPs → specific failure, not division by zero."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_000_000,
            measured_fpt=0.0,
            genesis_fpt=500_000_000.0,
        )
        assert result["verdict"] == "fail_zero_flops"

    def test_chain_keyed_baseline_lookup(self):
        """GENESIS_BASELINES map is used when ACTIVE_CHAIN_GENESIS_MODEL matches."""
        baselines = json.dumps({
            "Qwen/Qwen3-8B@abc123": 1.5e9,
            "testModel@testModelCommit": 1.0,
        })
        verifier, mv = _make_verifier(
            GENESIS_BASELINES=baselines,
            ACTIVE_CHAIN_GENESIS_MODEL="Qwen/Qwen3-8B@abc123",
            GENESIS_FLOPS_PER_TOKEN="0",  # Should be ignored in favour of map
        )
        fpt = mv.ModelVerifier._resolve_genesis_fpt()
        assert fpt == 1.5e9

    def test_chain_keyed_fallback_to_single_value(self):
        """If ACTIVE_CHAIN_GENESIS_MODEL not in map, fall back to GENESIS_FLOPS_PER_TOKEN."""
        verifier, mv = _make_verifier(
            GENESIS_BASELINES="{}",
            ACTIVE_CHAIN_GENESIS_MODEL="unknown@model",
            GENESIS_FLOPS_PER_TOKEN="750000000",
        )
        fpt = mv.ModelVerifier._resolve_genesis_fpt()
        assert fpt == 750_000_000.0

    def test_regtest_baseline(self):
        """Regtest model with FLOPs=1.0 means difficulty always passes."""
        baselines = json.dumps({"testModel@testModelCommit": 1.0})
        verifier, mv = _make_verifier(
            GENESIS_BASELINES=baselines,
            ACTIVE_CHAIN_GENESIS_MODEL="testModel@testModelCommit",
        )
        # With genesis_fpt=1.0 and normalizer=1M:
        # expected = 1M * 1.0 / 500M = ~0.002 — effectively anything passes
        # because the genesis is so small relative to real models
        fpt = mv.ModelVerifier._resolve_genesis_fpt()
        assert fpt == 1.0

    def test_report_contains_all_fields(self):
        """Verify _validate_difficulty returns all documented fields."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=1_000_000,
            measured_fpt=500_000_000.0,
            genesis_fpt=500_000_000.0,
        )
        expected_keys = {
            "expected_difficulty", "claimed_difficulty", "ratio",
            "within_tolerance", "tolerance", "normalizer",
            "genesis_flops_per_token", "measured_flops_per_token", "verdict",
        }
        assert expected_keys.issubset(result.keys())

    def test_large_model_small_difficulty(self):
        """10x FLOPs model → difficulty = 100K."""
        verifier, mv = _make_verifier()
        result = mv.ModelVerifier._validate_difficulty(
            claimed_difficulty=100_000,
            measured_fpt=5_000_000_000.0,  # 5B = 10x genesis
            genesis_fpt=500_000_000.0,
        )
        assert result["within_tolerance"]
        assert result["expected_difficulty"] == 100_000


# ---------------------------------------------------------------------------
# TestModelVerifierValidate — monkeypatched ModelAuditor
# ---------------------------------------------------------------------------

class TestModelVerifierValidate:
    """Test the validate() control flow with monkeypatched ModelAuditor."""

    def test_validate_returns_pending_with_report(self, monkeypatch):
        """Successful audit → ('pending_operator_review', report)."""
        verifier, mv = _make_verifier()

        monkeypatch.setattr(mv, "ModelAuditor", type("MockAuditor", (), {
            "__init__": lambda self, *a, **kw: None,
            "run_audit": lambda self, **kw: SAMPLE_REPORT.copy(),
        }))

        status, report = verifier.validate(
            raw_message=b"\x00" * 32,
            claimed_difficulty=500_000,  # Matches 2x FLOPs (1B vs 500M genesis)
            model_name="test/model",
        )
        assert status == "pending_operator_review"
        assert "difficulty_validation" in report
        assert report["flops"]["flops_per_token"] == 1e9

    def test_validate_returns_fail_on_exception(self, monkeypatch):
        """ModelAuditor crash → ('pending_operator_review', failure report with audit_exception)."""
        verifier, mv = _make_verifier()

        def _boom(*a, **kw):
            raise RuntimeError("CUDA out of memory")

        monkeypatch.setattr(mv, "ModelAuditor", type("BoomAuditor", (), {
            "__init__": _boom,
        }))

        status, report = verifier.validate(
            raw_message=b"\x00" * 32,
            claimed_difficulty=1_000_000,
            model_name="test/model",
        )
        assert status == "pending_operator_review"
        assert report["failure_reason"] == "audit_exception"
        assert report["failure_stage"] == "audit_run"
        assert report["audit_completed"] is False
        assert report["requires_operator_decision"] is True
        assert "CUDA out of memory" in report["error"]

    def test_validate_returns_fail_on_empty_model_name(self):
        """Empty model name → ('pending_operator_review', failure report with empty_model_name)."""
        verifier, mv = _make_verifier()
        status, report = verifier.validate(
            raw_message=b"\x00" * 32,
            claimed_difficulty=1_000_000,
            model_name="",
        )
        assert status == "pending_operator_review"
        assert report["failure_reason"] == "empty_model_name"
        assert report["failure_stage"] == "precheck"
        assert report["audit_completed"] is False
        assert report["requires_operator_decision"] is True
        assert "Empty model_name" in report["error"]

    def test_report_contains_difficulty_validation(self, monkeypatch):
        """Annotated report has difficulty_validation section with all fields."""
        verifier, mv = _make_verifier()

        monkeypatch.setattr(mv, "ModelAuditor", type("MockAuditor", (), {
            "__init__": lambda self, *a, **kw: None,
            "run_audit": lambda self, **kw: SAMPLE_REPORT.copy(),
        }))

        status, report = verifier.validate(
            raw_message=b"\x00" * 32,
            claimed_difficulty=500_000,
            model_name="test/model",
        )
        dv = report["difficulty_validation"]
        assert "expected_difficulty" in dv
        assert "claimed_difficulty" in dv
        assert "ratio" in dv
        assert "verdict" in dv
        assert dv["claimed_difficulty"] == 500_000

    def test_validate_includes_elapsed_time(self, monkeypatch):
        """Report includes audit_elapsed_ms."""
        verifier, mv = _make_verifier()

        monkeypatch.setattr(mv, "ModelAuditor", type("MockAuditor", (), {
            "__init__": lambda self, *a, **kw: None,
            "run_audit": lambda self, **kw: SAMPLE_REPORT.copy(),
        }))

        status, report = verifier.validate(
            raw_message=b"\x00" * 32,
            claimed_difficulty=500_000,
            model_name="test/model",
        )
        assert "audit_elapsed_ms" in report
        assert isinstance(report["audit_elapsed_ms"], int)
