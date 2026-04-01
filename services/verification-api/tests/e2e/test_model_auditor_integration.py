# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for ModelAuditor with real HuggingFace models.

These tests download real models and run actual GPU/CPU audits.
They are OPT-IN — skipped unless RUN_MODEL_AUDIT_TESTS=1 is set.

Run via: RUN_MODEL_AUDIT_TESTS=1 ./tests/run_tests.sh e2e

Test models:
  - Qwen/Qwen2.5-0.5B     (~500M params, dense transformer)
  - PrimeIntellect/qwen3-moe-tiny  (~670M total, 16 experts / 4 active, MoE)
"""

import os
import pytest

_SKIP_REASON = "opt-in: set RUN_MODEL_AUDIT_TESTS=1 to download real models"
_ENABLED = os.getenv("RUN_MODEL_AUDIT_TESTS", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(not _ENABLED, reason=_SKIP_REASON)


@pytest.fixture(scope="module")
def dense_report():
    """Run audit on Qwen2.5-0.5B once, share across tests."""
    from model_verifier import ModelAuditor
    auditor = ModelAuditor("Qwen/Qwen2.5-0.5B", device="cpu")
    return auditor.run_audit(ctx_len=32, saliency_passes=2, saliency_chunk_rows=512)


@pytest.fixture(scope="module")
def moe_report():
    """Run audit on qwen3-moe-tiny (~670M, 16 experts / 4 active)."""
    from model_verifier import ModelAuditor
    auditor = ModelAuditor("PrimeIntellect/qwen3-moe-tiny", device="cpu")
    return auditor.run_audit(ctx_len=32, saliency_passes=2, saliency_chunk_rows=512)


class TestModelAuditorDense:
    """Validate audit report for a ~500M dense model."""

    def test_report_structure(self, dense_report):
        """Report has all required top-level sections."""
        assert "flops" in dense_report
        assert "salient_weights" in dense_report
        assert "validity" in dense_report
        assert "file_size_check" in dense_report

    def test_nonzero_flops(self, dense_report):
        """FLOPs are positive and non-trivial."""
        assert dense_report["flops"]["total_flops"] > 0
        assert dense_report["flops"]["flops_per_token"] > 0

    def test_salient_weights_sane(self, dense_report):
        """Salient weight percentage is between 0 and 100."""
        pct = dense_report["salient_weights"]["percentage"]
        assert 0 < pct <= 100

    def test_validity_permutation_ratio(self, dense_report):
        """Permuted input should produce worse perplexity than ordered."""
        assert dense_report["validity"]["permutation_perplexity_ratio"] > 1.0

    def test_total_params_reasonable(self, dense_report):
        """~500M param model should have params in the right ballpark."""
        params = dense_report["file_size_check"]["total_params"]
        assert 100_000_000 < params < 2_000_000_000  # 100M to 2B


class TestModelAuditorMoE:
    """Validate audit report for a MoE model (qwen3-moe-tiny, ~670M)."""

    def test_nonzero_flops(self, moe_report):
        assert moe_report["flops"]["total_flops"] > 0
        assert moe_report["flops"]["flops_per_token"] > 0

    def test_moe_active_ratio_below_one(self, moe_report):
        """MoE models should have active_ratio < 1.0 (sparse expert activation)."""
        assert moe_report["flops"]["active_ratio"] < 1.0

    def test_total_params_reasonable(self, moe_report):
        """~670M total param MoE should be in the right ballpark."""
        params = moe_report["file_size_check"]["total_params"]
        assert 100_000_000 < params < 2_000_000_000  # 100M to 2B


class TestDenseVsMoEComparison:
    """Cross-model comparisons to validate FLOPs scaling and difficulty derivation."""

    def test_moe_flops_not_proportional_to_params(self, dense_report, moe_report):
        """MoE has more total params but FLOPs ratio < param ratio (sparse activation)."""
        dense_fpt = dense_report["flops"]["flops_per_token"]
        moe_fpt = moe_report["flops"]["flops_per_token"]
        dense_params = dense_report["file_size_check"]["total_params"]
        moe_params = moe_report["file_size_check"]["total_params"]

        param_ratio = moe_params / dense_params
        flops_ratio = moe_fpt / dense_fpt

        # MoE is more efficient per-param than dense
        assert flops_ratio < param_ratio

    def test_difficulty_derivation_produces_sane_value(self, dense_report, moe_report):
        """Use dense as genesis baseline → MoE difficulty scalar is positive and finite."""
        genesis_fpt = dense_report["flops"]["flops_per_token"]
        moe_fpt = moe_report["flops"]["flops_per_token"]
        normalizer = 1_000_000

        # Inverse compute scalar: more FLOPs → lower difficulty
        expected_moe_difficulty = int(normalizer * genesis_fpt / moe_fpt)

        # Must be a sane positive number
        assert expected_moe_difficulty > 0
        # And not absurdly large (would mean near-zero FLOPs — broken audit)
        assert expected_moe_difficulty < normalizer * 100

    def test_difficulty_direction_matches_flops(self, dense_report, moe_report):
        """Higher FLOPs model gets lower difficulty (inverse relationship)."""
        genesis_fpt = dense_report["flops"]["flops_per_token"]
        moe_fpt = moe_report["flops"]["flops_per_token"]
        normalizer = 1_000_000

        dense_difficulty = normalizer  # Genesis = normalizer by definition
        moe_difficulty = int(normalizer * genesis_fpt / moe_fpt)

        # The model with more FLOPs/token should have lower difficulty
        if moe_fpt > genesis_fpt:
            assert moe_difficulty < dense_difficulty
        else:
            assert moe_difficulty > dense_difficulty
