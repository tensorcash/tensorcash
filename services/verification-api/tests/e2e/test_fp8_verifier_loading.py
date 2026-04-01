# SPDX-License-Identifier: Apache-2.0
"""
Opt-in FP8 verifier loading tests.

These tests download real HuggingFace models and exercise the verifier's
existing CausalLM forward path. They are skipped unless RUN_FP8_VERIFIER_TESTS=1
is set.

Default model pair:
  - Base: Qwen/Qwen2-0.5B-Instruct
  - FP8 : RedHatAI/Qwen2-0.5B-Instruct-FP8
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


_ENABLED = os.getenv("RUN_FP8_VERIFIER_TESTS", "").lower() in {
    "1", "true", "yes"
}
_SKIP_REASON = (
    "opt-in: set RUN_FP8_VERIFIER_TESTS=1 to download and load real FP8 models"
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.fp8,
    pytest.mark.skipif(not _ENABLED, reason=_SKIP_REASON),
]

BASE_MODEL = os.getenv("FP8_VERIFIER_BASE_MODEL", "Qwen/Qwen2-0.5B-Instruct")
FP8_MODEL = os.getenv(
    "FP8_VERIFIER_FP8_MODEL", "RedHatAI/Qwen2-0.5B-Instruct-FP8"
)
BASE_REVISION = os.getenv("FP8_VERIFIER_BASE_REVISION", "main")
FP8_REVISION = os.getenv("FP8_VERIFIER_FP8_REVISION", "main")
REPLAY_DTYPE = os.getenv("FP8_VERIFIER_REPLAY_DTYPE", "bf16")


def _import_real_verifier():
    """Undo the lightweight pytest stubs and import the real verifier module."""
    os.environ.pop("CUDA_VISIBLE_DEVICES", None)
    os.environ["TEST_MODE"] = "false"
    torch_bound_modules = {
        "config.constants",
        "utils.shared_utils",
    }
    for name in list(sys.modules):
        if (
            name == "proof_verifier"
            or name in torch_bound_modules
            or name == "torch"
            or name.startswith("torch.")
        ):
            del sys.modules[name]
    return importlib.import_module("proof_verifier")


def test_small_pair_has_expected_quantization_configs():
    pv = _import_real_verifier()
    from transformers import AutoConfig

    base_cfg = AutoConfig.from_pretrained(
        BASE_MODEL, revision=BASE_REVISION, trust_remote_code=True
    )
    fp8_cfg = AutoConfig.from_pretrained(
        FP8_MODEL, revision=FP8_REVISION, trust_remote_code=True
    )

    assert not pv._config_is_fp8_quantized(base_cfg)
    assert pv._config_is_fp8_quantized(fp8_cfg)


def test_fp8_verifier_loads_with_replay_dtype_and_runs_forward():
    pv = _import_real_verifier()
    torch = importlib.import_module("torch")
    from transformers import AutoTokenizer

    if not torch.cuda.is_available():
        pytest.skip("FP8 verifier load test needs CUDA")

    verifier = pv.ProofVerifier()
    verifier.model_name = FP8_MODEL
    verifier.commit_hash = FP8_REVISION
    verifier.precision = "fp8"
    verifier.model_config_diff = {"replay_compute_dtype": REPLAY_DTYPE}
    verifier.dtype = pv._precision_replay_dtype(
        "fp8", proof_config_diff=verifier.model_config_diff
    )
    verifier.stated_precision = "fp8"
    verifier.stated_dtype = verifier.dtype
    verifier.ipfs_cid = None
    verifier.perform_smell_test = False

    assert not pv._is_fp8_dtype(verifier.dtype)
    assert verifier._load_model()

    assert pv._config_is_fp8_quantized(verifier.model.config)
    assert not pv._is_fp8_dtype(verifier.dtype)

    fp8_param_count = sum(
        1 for param in verifier.model.parameters()
        if pv._is_fp8_dtype(getattr(param, "dtype", None))
    )
    assert fp8_param_count > 0

    tokenizer = AutoTokenizer.from_pretrained(
        FP8_MODEL, revision=FP8_REVISION, trust_remote_code=True
    )
    inputs = tokenizer("The verifier fp8 smoke test", return_tensors="pt")
    model_device = next(verifier.model.parameters()).device
    inputs = {k: v.to(model_device) for k, v in inputs.items()}

    pv.mca_debug_reset()
    with torch.no_grad():
        outputs = verifier.model(**inputs, use_cache=False)

    logits = outputs.logits[:, -1, :]
    assert logits.is_floating_point()
    assert torch.isfinite(logits).all()

    dbg = pv.mca_debug_snapshot()
    assert dbg.get("hook_count", 0) > 0
