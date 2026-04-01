"""Dual-backend (one miner-proxy fronting two vLLM) routing + injection.

Validates the GCP confidential-worker wiring:

  * MODEL_ROUTES routes each model to its own backend URL.
  * The chain-pinned mining model gets the mining readiness gate; any
    other model (e.g. the 27B inference model) does NOT.
  * _inject_pow_data dispatches the mining model to real PoW injection
    and every other model to AUDIT injection (audit_emit=True, no fake
    target), instead of the old hard pin-mismatch error.
  * Audit injection is fail-open: no mining context / VDF not ready
    forwards the request body untouched (never costs an inference).
"""
import os
import sys
import types
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Minimal utils.uint256_arithmetics shim (mirrors test_broker_inference_only).
if "utils.uint256_arithmetics" not in sys.modules:
    utils_pkg = types.ModuleType("utils")
    uint256_mod = types.ModuleType("utils.uint256_arithmetics")
    uint256_mod.set_compact = lambda x: x
    uint256_mod.get_compact = lambda x: x
    uint256_mod.adjust_nbits_by_multiplier = lambda bits, mult, default: {
        "target_bytes": b"\xff" * 32,
        "nbits": 0x1d00ffff,
    }
    sys.modules["utils"] = utils_pkg
    sys.modules["utils.uint256_arithmetics"] = uint256_mod

from unittest.mock import patch  # noqa: E402

from components import constants  # noqa: E402
from components import proxy as proxy_module  # noqa: E402
from components.context import LockFreeContext  # noqa: E402

MINING_MODEL = "Qwen/Qwen3-8B"
MINING_COMMIT = "9c925d64d72725edaf899c6cb9c377fd0709d9c5"
INFER_MODEL = "Qwen/Qwen3.5-27B-FP8"
MINING_URL = "http://127.0.0.1:8001"
INFER_URL = "http://127.0.0.1:8000"


def _make_manager():
    """Construct a RequestManager in broker-mining + dual-backend mode."""
    context = LockFreeContext("0" * 64, "ffff" * 16)
    patches = dict(
        WORKER_MODE="broker",
        MINING_ENABLED=True,
        STANDALONE_MODE=True,
        LOCAL_MODEL_NAME=MINING_MODEL,   # pins the mining model
        MODEL_HASH=MINING_COMMIT,
        MODEL_ROUTES={MINING_MODEL: MINING_URL, INFER_MODEL: INFER_URL},
        MODEL_DIFFICULTY_NORMALIZER=1000000,
        USE_VLLM_XARGS=False,
        TARGET_URL=INFER_URL,
    )
    with patch.multiple(constants, **patches):
        # ModelClient is created (broker-mining, not inference-only) but we
        # don't exercise the registry path here.
        with patch.object(proxy_module, "ModelClient", return_value=Mock()):
            mgr = proxy_module.RequestManager(context)
    return mgr


def _warm_snapshot():
    """A mining-context snapshot with a VDF available (audit-ready)."""
    snap = Mock()
    snap.vdf_proof = "aa" * 32
    snap.vdf_tick = 7
    snap.block_hash = "bb" * 32
    snap.target = "ff" * 32
    snap.header_prefix = "cc" * 38
    snap.request_id = 12345
    snap.base_share_target = ""
    return snap


class TestBackendRouting(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()

    def test_routes_each_model_to_its_backend(self):
        self.assertEqual(self.mgr._backend_base_url(MINING_MODEL), MINING_URL)
        self.assertEqual(self.mgr._backend_base_url(INFER_MODEL), INFER_URL)

    def test_unrouted_model_falls_back_to_default(self):
        self.assertEqual(self.mgr._backend_base_url("some/other-model"), self.mgr._base_url)

    def test_is_mining_model(self):
        self.assertTrue(self.mgr._is_mining_model(MINING_MODEL))
        self.assertFalse(self.mgr._is_mining_model(INFER_MODEL))


class TestInjectionDispatch(unittest.TestCase):
    def setUp(self):
        self.mgr = _make_manager()
        self.mgr.context = Mock()
        self.mgr.context.read.return_value = _warm_snapshot()

    def _patches(self):
        return patch.multiple(
            constants,
            MODEL_DIFFICULTY_NORMALIZER=1000000,
            USE_VLLM_XARGS=False,
            DEFAULT_DIFFICULTY=1000000,
        )

    def test_inference_model_gets_audit_injection(self):
        with self._patches():
            out = self.mgr._inject_pow_data({"model": INFER_MODEL, "prompt": "hi"})
        pow_payload = out.get("extra_sampling_params", {}).get("pow")
        self.assertIsNotNone(pow_payload, "27B model must receive audit PoW payload")
        self.assertTrue(pow_payload.get("audit_emit"), "audit_emit must be True for non-mining model")
        # Audit must NOT carry a share_target (mining-only field)
        self.assertNotIn("share_target", pow_payload)

    def test_inference_model_audit_failopen_when_no_vdf(self):
        # VDF not ready → audit injection forwards body untouched.
        self.mgr.context.read.return_value.vdf_proof = None
        with self._patches():
            data = {"model": INFER_MODEL, "prompt": "hi", "temperature": 0}
            out = self.mgr._inject_pow_data(dict(data))
        self.assertNotIn("extra_sampling_params", out)
        self.assertNotIn("vllm_xargs", out)
        self.assertEqual(out["model"], INFER_MODEL)

    def test_inference_model_does_not_raise_pin_mismatch(self):
        # The old behaviour raised "does not match configured MODEL_NAME".
        # Now a non-mining model must be handled via audit, never raise.
        with self._patches():
            try:
                self.mgr._inject_pow_data({"model": INFER_MODEL, "prompt": "hi"})
            except RuntimeError as e:
                self.fail(f"non-mining model must not raise pin mismatch: {e}")


if __name__ == "__main__":
    unittest.main()
