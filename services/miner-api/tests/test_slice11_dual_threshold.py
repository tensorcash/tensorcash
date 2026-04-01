"""Slice 11 — worker-side helpers + emission policy tests.

Pins:

  a. ``derive_adjusted_share_target`` math agrees BIT-EXACTLY with
     the broker's ``VerifyServiceShareClient._compute_adjusted_target``
     (same formula, same saturation cap at ``2**256 - 1``).
  b. ``MiningTemplate.from_dict`` accepts the broker's new
     ``base_share_target`` field AND the legacy ``share_target``
     fallback during rollout.
  c. ``_forward_solution`` emits ONLY MineResult — block-hit
     shares are now credited by the broker internally (see
     ShareVerifier.credit_block_hit_share). Previously the worker
     emitted MineShare alongside MineResult, which raced with
     lease closure and rejected as ``lease_inactive``.
  d. ``_forward_share`` math + fail-soft paths still test the
     helper directly (it's the entry point for the FUTURE
     sub-block-above-share emission path, once the C++ miner
     starts emitting those proofs).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Same stub the existing mining-gate tests install so we don't have
# to wire the full uint256 lib for this narrow surface.
if "utils.uint256_arithmetics" not in sys.modules:
    utils_pkg = types.ModuleType("utils")
    uint256_mod = types.ModuleType("utils.uint256_arithmetics")
    uint256_mod.set_compact = lambda x: x
    uint256_mod.get_compact = lambda x: x
    uint256_mod.adjust_nbits_by_multiplier = lambda *a, **k: {
        "target_bytes": b"\xff" * 32,
        "nbits": 0x1d00ffff,
    }
    sys.modules["utils"] = utils_pkg
    sys.modules["utils.uint256_arithmetics"] = uint256_mod

# ``components.proof_collector`` pulls in zmq and the proof
# FlatBuffer module; neither is available in the local test venv.
# Stub the module surface to expose just the two byte-level helpers
# ``_forward_share`` reaches for. The classification path under
# test runs in-process via direct method calls — the real
# proof_collector ZMQ loop never spins up here.
if "components.proof_collector" not in sys.modules:
    pc_stub = types.ModuleType("components.proof_collector")
    pc_stub._extract_proof_hash_hex = lambda buf: ("ab" * 32)
    pc_stub._extract_proof_nonce = lambda buf: 0x12345678
    # ProofCollector class isn't used in these tests; provide a
    # placeholder so any incidental import succeeds.
    class _ProofCollectorStub:
        def __init__(self, *a, **kw): pass
        def set_solution_callback(self, *a, **kw): pass
    pc_stub.ProofCollector = _ProofCollectorStub
    sys.modules["components.proof_collector"] = pc_stub

from components import constants  # noqa: E402
from components.mining_protocol import (  # noqa: E402
    MiningProtocolError,
    MiningTemplate,
    derive_adjusted_share_target,
)


# ---------------------------------------------------------------- a
# Adjusted target math


class TestDeriveAdjustedShareTarget(unittest.TestCase):
    """The worker's adjusted-target math MUST agree byte-exactly with
    the broker's. Both implementations compute
    ``floor(base * normalizer / difficulty)`` over Python ints; this
    test pins the formula and the cap behaviour."""

    def test_basic_formula(self):
        # Trivial sanity: base=1024, N=1_000_000, D=2_000_000 →
        # floor(1024 * 1_000_000 / 2_000_000) = 512.
        out = derive_adjusted_share_target(
            base_share_target_hex=f"{1024:064x}",
            normalizer=1_000_000,
            difficulty=2_000_000,
        )
        self.assertEqual(int(out, 16), 512)

    def test_matches_bcore_split_recombine_equivalent(self):
        # bcore reference: adj = q*N + floor(rem*N/D); Python:
        # adj = (base * N) // D. Mathematically identical for any
        # 256-bit input. Pin against a realistic share-mode value.
        base = int("00" * 16 + "ff" * 16, 16)
        N = 1_000_000
        D = 1_500_000
        out = derive_adjusted_share_target(
            base_share_target_hex=f"{base:064x}",
            normalizer=N, difficulty=D,
        )
        expected = (base * N) // D
        q = base // D
        rem = base % D
        split = q * N + (rem * N) // D
        self.assertEqual(int(out, 16), expected)
        self.assertEqual(expected, split)

    def test_saturates_at_2_pow_256_minus_one(self):
        # Easy model (D < N) blows the threshold past 256 bits.
        # Must cap, not wrap.
        base = (1 << 255) | 1
        out = derive_adjusted_share_target(
            base_share_target_hex=f"{base:064x}",
            normalizer=10_000_000, difficulty=1_000_000,
        )
        self.assertEqual(int(out, 16), (1 << 256) - 1)

    def test_emits_64_char_lowercase_hex(self):
        out = derive_adjusted_share_target(
            base_share_target_hex="ff" * 32,
            normalizer=1_000_000, difficulty=1_000_000,
        )
        self.assertEqual(len(out), 64)
        self.assertEqual(out, out.lower())

    def test_zero_difficulty_raises(self):
        with self.assertRaises(MiningProtocolError):
            derive_adjusted_share_target(
                base_share_target_hex="00" * 31 + "01",
                normalizer=1_000_000, difficulty=0,
            )

    def test_zero_normalizer_raises(self):
        with self.assertRaises(MiningProtocolError):
            derive_adjusted_share_target(
                base_share_target_hex="00" * 31 + "01",
                normalizer=0, difficulty=1,
            )

    def test_empty_base_raises(self):
        with self.assertRaises(MiningProtocolError):
            derive_adjusted_share_target(
                base_share_target_hex="",
                normalizer=1, difficulty=1,
            )

    def test_non_hex_base_raises(self):
        with self.assertRaises(MiningProtocolError):
            derive_adjusted_share_target(
                base_share_target_hex="not-hex",
                normalizer=1, difficulty=1,
            )


# ---------------------------------------------------------------- b
# Wire-field rollout (base_share_target ← share_target)


class TestMiningTemplateWireField(unittest.TestCase):
    def _template_raw(self, **overrides):
        base = dict(
            template_id="tmpl-1",
            request_id=1,
            block_hash="aa" * 32,
            header_prefix="00" * 76,
            target="ff" * 32,
            bits=0x1d00ffff,
            expires_at=9999999999,
        )
        base.update(overrides)
        return base

    def test_accepts_new_base_share_target_field(self):
        # New broker → new worker. Field name matches the broker's
        # outbound JSON in mining_protocol.py.
        tmpl = MiningTemplate.from_dict(self._template_raw(
            base_share_target="ff" * 32,
            share_shift_bits=8,
        ))
        self.assertEqual(tmpl.base_share_target, "ff" * 32)
        self.assertEqual(tmpl.share_shift_bits, 8)

    def test_legacy_share_target_field_still_parses(self):
        # Rollout compat: a hypothetical older broker emitting the
        # pre-slice-11 ``share_target`` key still produces a worker
        # template with base_share_target populated. Remove this
        # branch once every broker is past slice 11.
        tmpl = MiningTemplate.from_dict(self._template_raw(
            share_target="aa" * 32,
        ))
        self.assertEqual(tmpl.base_share_target, "aa" * 32)

    def test_new_field_wins_when_both_present(self):
        # Defensive: if both keys are on the wire, prefer the new
        # one (the broker's authoritative slice-11 field).
        tmpl = MiningTemplate.from_dict(self._template_raw(
            base_share_target="11" * 32,
            share_target="22" * 32,
        ))
        self.assertEqual(tmpl.base_share_target, "11" * 32)

    def test_missing_share_field_yields_none(self):
        tmpl = MiningTemplate.from_dict(self._template_raw())
        self.assertIsNone(tmpl.base_share_target)
        self.assertIsNone(tmpl.share_shift_bits)


# ---------------------------------------------------------------- c, d
# _forward_share end-to-end behaviour


def _install_worker_client():
    from worker_client import BrokerWorkerClient  # noqa: E402
    return BrokerWorkerClient


def _make_request(*, base_share_target=None, model_name="llama", model_commit="abc123"):
    """Build a typed MineRequest the way the broker would send it."""
    from components.mining_protocol import MineRequest
    raw = {
        "type": "MINE_REQUEST",
        "job_id": "job-1",
        "work_unit_id": 7,
        "wallet_id": "wallet-x",
        "network": "tensor-test",
        "mode": "request_attached",
        "model": {
            "name": model_name,
            "commit": model_commit,
            "model_hash": "deadbeef" * 8,
        },
        "template": {
            "template_id": "tmpl-1",
            "request_id": 7,
            "block_hash": "aa" * 32,
            "header_prefix": "00" * 76,
            "target": "00" * 31 + "ff",  # tight block target
            "bits": 0x1d00ffff,
            "expires_at": 9999999999,
            **({"base_share_target": base_share_target} if base_share_target else {}),
            "share_shift_bits": 8,
        },
        "policy": {"submit_policy": "tensorcash_core_node"},
    }
    return MineRequest.from_dict(raw)


class TestForwardSolutionBlockHitOnly(unittest.IsolatedAsyncioTestCase):
    """Slice 11 revision: ``_forward_solution`` emits ONLY
    MineResult. The block-hit share is credited by the broker
    side (no race with lease closure).
    """

    def _build_client(self, *, with_request_manager=True, with_difficulty=1_000_000):
        BrokerWorkerClient = _install_worker_client()
        from components.context import LockFreeContext
        ctx = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "MINING_ENABLED", True), \
             patch.object(constants, "WORKER_TOOLS", []), \
             patch.object(constants, "WORKER_SUPPORTED_MODES", ["plaintext"]):
            request_manager = None
            if with_request_manager:
                model_client = MagicMock()
                model_client.get_model_by_name_and_commit = MagicMock(
                    return_value=(
                        {"difficulty": with_difficulty, "model_hash": "h"}
                        if with_difficulty
                        else None
                    ),
                )
                request_manager = MagicMock()
                request_manager.model_client = model_client
            client = BrokerWorkerClient(
                context=ctx,
                zmq_listener=MagicMock(),
                proof_collector=None,
                request_manager=request_manager,
            )
        client.ws = AsyncMock()
        client._loop = None
        return client

    async def test_block_hit_emits_only_mine_result(self):
        # Slice 11 revision: a block hit emits ONLY MineResult.
        # The matching share credit is the broker's responsibility
        # (ShareVerifier.credit_block_hit_share runs in the
        # broker's MineResult handler AFTER chain submit, BEFORE
        # lease close — see mining_scheduler._await_and_route).
        # The previous design where the worker emitted MineShare
        # alongside MineResult raced with lease closure and got
        # REJECT_LEASE_INACTIVE on the broker side.
        client = self._build_client()
        request = _make_request(base_share_target="ff" * 32)
        client._mining_in_flight[7] = request
        client.mining_request_mapping[7] = "job-1"
        client.mining_job_mapping["job-1"] = 7

        with patch.object(constants, "MODEL_DIFFICULTY_NORMALIZER", 1_000_000):
            await client._forward_solution(
                job_id="job-1", req_id=7,
                result_b64=base64.b64encode(b"mining-buf-bytes").decode(),
            )

        sent_frames = [
            json.loads(call.args[0]) for call in client.ws.send.await_args_list
        ]
        types_sent = [f.get("type") for f in sent_frames]
        self.assertEqual(types_sent, ["MINE_RESULT"], (
            f"worker block-hit path must emit ONLY MineResult; "
            f"sent={types_sent}. Block-hit shares are credited by "
            "the broker (no race) via "
            "ShareVerifier.credit_block_hit_share."
        ))


class TestForwardShareSubBlockHelper(unittest.IsolatedAsyncioTestCase):
    """``_forward_share`` is the entry point for the FUTURE
    sub-block-above-share emission path (once the C++ miner starts
    emitting those proofs). It's no longer reached from
    ``_forward_solution`` (block hits are broker-credited).

    These tests pin the helper's behaviour directly so the future
    sub-block path has a working entry point — the math, fail-soft
    paths, and the adjusted-target derivation are all still
    correct.
    """

    def _build_client(self, *, with_request_manager=True, with_difficulty=1_000_000):
        BrokerWorkerClient = _install_worker_client()
        from components.context import LockFreeContext
        ctx = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "MINING_ENABLED", True), \
             patch.object(constants, "WORKER_TOOLS", []), \
             patch.object(constants, "WORKER_SUPPORTED_MODES", ["plaintext"]):
            request_manager = None
            if with_request_manager:
                model_client = MagicMock()
                model_client.get_model_by_name_and_commit = MagicMock(
                    return_value=(
                        {"difficulty": with_difficulty, "model_hash": "h"}
                        if with_difficulty
                        else None
                    ),
                )
                request_manager = MagicMock()
                request_manager.model_client = model_client
            client = BrokerWorkerClient(
                context=ctx,
                zmq_listener=MagicMock(),
                proof_collector=None,
                request_manager=request_manager,
            )
        client.ws = AsyncMock()
        client._loop = None
        return client

    async def test_direct_share_emission_uses_adjusted_target(self):
        # When the helper IS called (future sub-block emission),
        # the share_target field on the wire MUST be the
        # adjusted target, not the base.
        client = self._build_client(with_difficulty=2_000_000)
        request = _make_request(base_share_target=f"{1024:064x}")
        client._mining_in_flight[7] = request

        with patch.object(constants, "MODEL_DIFFICULTY_NORMALIZER", 1_000_000):
            await client._forward_share(
                job_id="job-1", req_id=7,
                mining_buf=b"buf",
                share_b64=base64.b64encode(b"buf").decode(),
            )

        sent_frames = [
            json.loads(call.args[0]) for call in client.ws.send.await_args_list
        ]
        share_frame = next(f for f in sent_frames if f["type"] == "MINE_SHARE")
        # floor(1024 * 1_000_000 / 2_000_000) = 512.
        self.assertEqual(int(share_frame["share_target"], 16), 512)

    async def test_no_base_share_target_skips(self):
        client = self._build_client()
        request = _make_request(base_share_target=None)
        client._mining_in_flight[7] = request
        with patch.object(constants, "MODEL_DIFFICULTY_NORMALIZER", 1_000_000):
            await client._forward_share(
                job_id="job-1", req_id=7,
                mining_buf=b"buf",
                share_b64=base64.b64encode(b"buf").decode(),
            )
        self.assertEqual(client.ws.send.await_args_list, [])

    async def test_normalizer_zero_skips(self):
        client = self._build_client()
        request = _make_request(base_share_target="ff" * 32)
        client._mining_in_flight[7] = request
        with patch.object(constants, "MODEL_DIFFICULTY_NORMALIZER", 0):
            await client._forward_share(
                job_id="job-1", req_id=7,
                mining_buf=b"buf",
                share_b64=base64.b64encode(b"buf").decode(),
            )
        self.assertEqual(client.ws.send.await_args_list, [])

    async def test_no_request_manager_skips(self):
        client = self._build_client(with_request_manager=False)
        request = _make_request(base_share_target="ff" * 32)
        client._mining_in_flight[7] = request
        with patch.object(constants, "MODEL_DIFFICULTY_NORMALIZER", 1_000_000):
            await client._forward_share(
                job_id="job-1", req_id=7,
                mining_buf=b"buf",
                share_b64=base64.b64encode(b"buf").decode(),
            )
        self.assertEqual(client.ws.send.await_args_list, [])

    async def test_unknown_model_skips(self):
        client = self._build_client(with_difficulty=None)
        request = _make_request(base_share_target="ff" * 32)
        client._mining_in_flight[7] = request
        with patch.object(constants, "MODEL_DIFFICULTY_NORMALIZER", 1_000_000):
            await client._forward_share(
                job_id="job-1", req_id=7,
                mining_buf=b"buf",
                share_b64=base64.b64encode(b"buf").decode(),
            )
        self.assertEqual(client.ws.send.await_args_list, [])


class TestOnSolutionReceivedClassifier(unittest.IsolatedAsyncioTestCase):
    """Slice 11.4 — ``_on_solution_received`` classifies on
    ``Proof.is_solution`` and routes accordingly:

      - True  → ``_forward_solution`` (MineResult; broker credits share)
      - False → ``_forward_share`` (sub-block share emission)

    Until the underlying sampler ships sub-block emission this path
    only fires for block hits; the test pins the classifier
    contract so future sampler emissions land cleanly without
    further worker-side wiring.
    """

    def _build_client(self):
        BrokerWorkerClient = _install_worker_client()
        from components.context import LockFreeContext
        ctx = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "MINING_ENABLED", True), \
             patch.object(constants, "WORKER_TOOLS", []), \
             patch.object(constants, "WORKER_SUPPORTED_MODES", ["plaintext"]):
            client = BrokerWorkerClient(
                context=ctx,
                zmq_listener=MagicMock(),
                proof_collector=None,
                request_manager=None,
            )
        # Real loop so run_coroutine_threadsafe works on the test's
        # event loop.
        import asyncio as _aio
        try:
            client._loop = _aio.get_running_loop()
        except RuntimeError:
            client._loop = _aio.new_event_loop()
        client.running = True
        client._forward_solution = AsyncMock()
        client._forward_share = AsyncMock()
        client.mining_request_mapping = {42: "job-42"}
        return client

    async def test_is_solution_true_routes_to_forward_solution(self, monkeypatch=None):
        import sys as _sys
        client = self._build_client()
        # Stub the classifier helper to return True (block solution).
        pc_mod = _sys.modules["components.proof_collector"]
        orig = getattr(pc_mod, "_extract_is_solution", None)
        try:
            pc_mod._extract_is_solution = lambda buf: True
            client._on_solution_received(req_id=42, mining_buf=b"buf")
            # The schedule call is fire-and-forget — yield to the
            # loop to let it complete the task.
            import asyncio as _aio
            await _aio.sleep(0)
        finally:
            if orig is not None:
                pc_mod._extract_is_solution = orig
        client._forward_solution.assert_called_once()
        client._forward_share.assert_not_called()

    async def test_is_solution_false_routes_to_forward_share(self):
        import sys as _sys
        client = self._build_client()
        pc_mod = _sys.modules["components.proof_collector"]
        orig = getattr(pc_mod, "_extract_is_solution", None)
        try:
            pc_mod._extract_is_solution = lambda buf: False
            client._on_solution_received(req_id=42, mining_buf=b"buf")
            import asyncio as _aio
            await _aio.sleep(0)
        finally:
            if orig is not None:
                pc_mod._extract_is_solution = orig
        # NEW path: sub-block share emission.
        client._forward_share.assert_called_once()
        client._forward_solution.assert_not_called()

    async def test_classifier_failure_falls_back_to_block_path(self):
        # Parser raises (e.g. corrupted FlatBuffer): default to the
        # legacy "treat as block solution" path so block hits never
        # silently disappear during the slice-11 rollout.
        import sys as _sys
        client = self._build_client()
        pc_mod = _sys.modules["components.proof_collector"]
        orig = getattr(pc_mod, "_extract_is_solution", None)
        try:
            def _raises(_buf):
                raise RuntimeError("bad FB")
            pc_mod._extract_is_solution = _raises
            client._on_solution_received(req_id=42, mining_buf=b"buf")
            import asyncio as _aio
            await _aio.sleep(0)
        finally:
            if orig is not None:
                pc_mod._extract_is_solution = orig
        client._forward_solution.assert_called_once()
        client._forward_share.assert_not_called()


if __name__ == "__main__":
    unittest.main()
