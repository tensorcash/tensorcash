"""Hardening tests for ``BrokerWorkerClient`` mining gates.

Two narrow guarantees are checked here:

1. ``_handle_mine_request`` must drop broker mining jobs cleanly when
   ``MINING_ENABLED=false`` (or when no LockFreeContext is attached). It
   must not touch ``zmq_listener._process_mining_job``, must not mutate
   ``LockFreeContext``, and must answer the broker with a
   ``MINE_RESULT(error=mining_disabled)`` so the lease can be released.

2. The HELLO advertisement must not include ``pow_injection`` in
   ``capabilities.features`` when mining is disabled. When enabled,
   the legacy advertisement is preserved.
"""
import base64
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure src and local_mocks are on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Install minimal utils mock before importing components / worker_client
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

from components import constants  # noqa: E402
from components.context import LockFreeContext  # noqa: E402
from worker_client import BrokerWorkerClient  # noqa: E402


def _make_client(*, mining_enabled: bool, with_context: bool = True, with_zmq: bool = True):
    """Build a ``BrokerWorkerClient`` under controlled mining state.

    The constructor reads ``constants.MINING_ENABLED`` once, so tests must
    patch it BEFORE instantiation. ``mining_enabled`` is the *intended*
    final state of ``self.mining_enabled``; we set MINING_ENABLED and the
    presence of context to match.
    """
    ctx = LockFreeContext("0" * 64, "ffff" * 16) if with_context else None
    zmq = MagicMock() if with_zmq else None
    with patch.object(constants, "MINING_ENABLED", mining_enabled), \
         patch.object(constants, "WORKER_TOOLS", []), \
         patch.object(constants, "WORKER_SUPPORTED_MODES", ["plaintext"]):
        client = BrokerWorkerClient(context=ctx, zmq_listener=zmq, proof_collector=None)
    client.ws = AsyncMock()
    return client, ctx, zmq


class TestMineRequestGuard(unittest.IsolatedAsyncioTestCase):
    """``_handle_mine_request`` must refuse work when mining is disabled."""

    async def test_mining_disabled_drops_request_and_returns_error(self):
        client, ctx, zmq = _make_client(mining_enabled=False)
        self.assertFalse(client.mining_enabled)

        before = ctx.read()
        msg = {
            "type": "MINE_REQUEST",
            "job_id": "mine-abc",
            "format": "fb",
            "payload_b64": base64.b64encode(b"would-be-flatbuffer").decode(),
        }
        await client._handle_mine_request(msg)

        # zmq_listener must NOT be touched
        zmq._process_mining_job.assert_not_called()

        # No job mapping was recorded
        self.assertEqual(client.mining_job_mapping, {})
        self.assertEqual(client.mining_request_mapping, {})

        # LockFreeContext must be unchanged
        after = ctx.read()
        self.assertEqual(before.block_hash, after.block_hash)
        self.assertEqual(before.request_id, after.request_id)
        self.assertEqual(before.vdf_proof, after.vdf_proof)
        self.assertEqual(before.vdf_tick, after.vdf_tick)

        # Broker is told the lease can close
        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MINE_RESULT")
        self.assertEqual(sent["job_id"], "mine-abc")
        self.assertEqual(sent["error"], "mining_disabled")

    async def test_mining_disabled_without_context_still_guards(self):
        # ``mining_enabled`` is also False when ``context is None``.
        client, _ctx, _zmq = _make_client(
            mining_enabled=True, with_context=False, with_zmq=False
        )
        self.assertFalse(client.mining_enabled)

        msg = {"type": "MINE_REQUEST", "job_id": "mine-xyz", "payload_b64": "AAAA"}
        await client._handle_mine_request(msg)

        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MINE_RESULT")
        self.assertEqual(sent["error"], "mining_disabled")

    async def test_mining_enabled_processes_request(self):
        """Phase 3 typed path: enabled worker validates the typed wire
        shape, builds a BlockHeader FlatBuffer from template.header_prefix,
        and forwards to zmq_listener._process_mining_job. Tracking is
        populated; no MINE_RESULT is sent until a solution arrives."""
        client, _ctx, zmq = _make_client(mining_enabled=True)
        self.assertTrue(client.mining_enabled)

        msg = {
            "type": "MINE_REQUEST",
            "job_id": "mine-ok",
            "work_unit_id": 42,
            "wallet_id": "tensorcash-test",
            "network": "test",
            "mode": "dummy_only",
            "model": {"name": "Qwen/Qwen3-8B", "commit": "0" * 40},
            "template": {
                "template_id": "tmpl_test",
                "request_id": 42,
                "block_hash": "aa" * 32,
                "header_prefix": "cd" * 76,
                "target": "ff" * 32,
                "bits": 0x207fffff,
                "expires_at": 1_700_000_000,
            },
            "policy": {
                "submit_policy": "tensorcash_core_node",
                "max_parallel": 1,
                "user_inference_blocks_on_mining": False,
            },
        }
        await client._handle_mine_request(msg)

        # zmq_listener got a real BlockHeader FlatBuffer (not the
        # base64-decoded payload from the v0 shape).
        zmq._process_mining_job.assert_called_once()
        self.assertEqual(client.mining_job_mapping["mine-ok"], 42)
        self.assertEqual(client.mining_request_mapping[42], "mine-ok")
        self.assertIn(42, client._mining_in_flight)

        # No MINE_RESULT yet — solution arrives via proof_collector later.
        client.ws.send.assert_not_awaited()

    async def test_mine_request_without_job_id_is_ignored(self):
        """Existing protocol guard preserved: missing job_id → silent drop."""
        client, _ctx, _zmq = _make_client(mining_enabled=False)
        await client._handle_mine_request({"type": "MINE_REQUEST"})
        client.ws.send.assert_not_awaited()


class TestHelloAdvertisement(unittest.IsolatedAsyncioTestCase):
    """HELLO must drop ``pow_injection`` when mining is not actually wired."""

    async def _capture_hello(self, client):
        # Legacy tests run before MAX_CONTEXT_WINDOW_EXPLICIT was a gate;
        # they don't care about context introspection. Pin the explicit
        # flag so HELLO doesn't hit the new "refuse silent 128k" guard,
        # and stub _get_models_with_context to skip the retry loop.
        with patch.object(constants, "MAX_CONTEXT_WINDOW_EXPLICIT", True), \
             patch.object(
                 client,
                 "_get_models_with_context",
                 new=AsyncMock(return_value={}),
             ), \
             patch.object(client, "_get_available_models",
                          new=AsyncMock(return_value=["test-model"])):
            await client._send_hello()
        client.ws.send.assert_awaited_once()
        return json.loads(client.ws.send.await_args.args[0])

    async def test_hello_when_disabled_omits_pow_injection(self):
        client, _, _ = _make_client(mining_enabled=False)
        msg = await self._capture_hello(client)

        caps = msg["capabilities"]
        self.assertEqual(msg["type"], "HELLO")
        self.assertIn("streaming", caps["features"])
        self.assertIn("responses", caps["features"])
        self.assertNotIn("pow_injection", caps["features"])
        self.assertNotIn("mining", caps)

    async def test_hello_when_enabled_includes_pow_injection_and_mining(self):
        client, _, _ = _make_client(mining_enabled=True)
        with patch.object(constants, "MINING_NETWORKS", ["test", "regtest"]), \
             patch.object(constants, "MINING_MAX_PARALLEL", 2):
            msg = await self._capture_hello(client)

        caps = msg["capabilities"]
        self.assertEqual(caps["features"], ["streaming", "pow_injection", "responses"])

        # Phase 3 v2 capability shape — the broker scheduler reads these
        # to decide if it can dispatch mining work to this worker.
        mining = caps["mining"]
        self.assertEqual(mining["enabled"], True)
        self.assertEqual(mining["schema_version"], 2)
        self.assertEqual(mining["networks"], ["test", "regtest"])
        # Worker advertises both modes when mining is enabled — proxy.py
        # already supports the dummy loop and PoW injection on real
        # requests.
        self.assertEqual(
            sorted(mining["supported_modes"]),
            ["dummy_only", "request_attached"],
        )
        # Slice 9+: BOTH wired. Broker registry sync feeds
        # ``MODEL_REGISTRY_SYNC`` frames into
        # ``ModelClient.update_from_payload``; solution return is the
        # ``MINE_RESULT`` path on _forward_solution. The dispatch gate
        # on the broker side REQUIRES supports_broker_registry=True
        # before sending share work, so this advertisement is load-
        # bearing — don't drop it without coordinating the broker.
        self.assertEqual(mining["supports_broker_registry"], True)
        self.assertEqual(mining["supports_solution_return"], True)
        self.assertEqual(mining["max_parallel"], 2)

    async def test_hello_when_enabled_with_no_networks_advertises_empty_list(self):
        """A worker with MINING_ENABLED=true but MINING_NETWORKS unset
        advertises networks=[]; the scheduler MUST treat that as "do
        not dispatch mining work to this worker" — we trust the operator
        forgot to opt the worker into a chain rather than guessing one.
        """
        client, _, _ = _make_client(mining_enabled=True)
        with patch.object(constants, "MINING_NETWORKS", []):
            msg = await self._capture_hello(client)
        self.assertEqual(msg["capabilities"]["mining"]["networks"], [])

    async def test_hello_mining_modes_do_not_clobber_inference_modes(self):
        """Regression: top-level capabilities.modes is the inference-routing
        namespace (plaintext / confidential) the broker uses for model
        traffic. Mining modes (dummy_only / request_attached) belong under
        capabilities.mining.supported_modes and must not leak into the
        top-level modes field even when the worker is both confidential-
        capable and mining-enabled.
        """
        client, _, _ = _make_client(mining_enabled=True)
        # Simulate confidential-capable worker: crypto_service present + flag on.
        client.crypto_service = MagicMock()
        with patch.object(constants, "CONFIDENTIAL_MODE_ENABLED", True):
            msg = await self._capture_hello(client)

        caps = msg["capabilities"]
        # Inference modes must stay plaintext/confidential, NOT mining modes.
        self.assertEqual(sorted(caps["modes"]), ["confidential", "plaintext"])
        # Mining modes still live in their own namespace.
        self.assertEqual(
            sorted(caps["mining"]["supported_modes"]),
            ["dummy_only", "request_attached"],
        )

    async def test_hello_inference_modes_when_mining_only(self):
        """Mining-enabled worker WITHOUT confidential crypto must still
        advertise plaintext (and only plaintext) as the top-level inference
        mode. The mining-mode names must never appear here.
        """
        client, _, _ = _make_client(mining_enabled=True)
        client.crypto_service = None
        msg = await self._capture_hello(client)

        caps = msg["capabilities"]
        self.assertEqual(caps["modes"], ["plaintext"])
        self.assertNotIn("dummy_only", caps["modes"])
        self.assertNotIn("request_attached", caps["modes"])


class TestHelloContextIntrospection(unittest.IsolatedAsyncioTestCase):
    """Positive paths for the vllm/llama backend introspection that drives
    HELLO's max_context_window. Without these, regressions on the helper
    silently re-introduce the historic 128000 lie even when /v1/models works.
    """

    async def _capture_hello_with_introspection(self, client, models_info):
        with patch.object(
            client,
            "_get_models_with_context",
            new=AsyncMock(return_value=models_info),
        ):
            await client._send_hello()
        client.ws.send.assert_awaited_once()
        return json.loads(client.ws.send.await_args.args[0])

    async def test_hello_uses_vllm_max_model_len(self):
        """Single-model vllm worker → advertise that model's max_model_len."""
        client, _, _ = _make_client(mining_enabled=False)
        msg = await self._capture_hello_with_introspection(
            client, {"Qwen/Qwen3-8B": 16384}
        )
        self.assertEqual(msg["models"], ["Qwen/Qwen3-8B"])
        self.assertEqual(msg["capabilities"]["max_context_window"], 16384)
        self.assertEqual(msg["capabilities"]["max_context_tokens"], 16384)

    async def test_hello_picks_min_context_across_models(self):
        """Multi-model worker → broker scheduler must use the smallest
        context to avoid over-routing to whichever model is loaded next."""
        client, _, _ = _make_client(mining_enabled=False)
        msg = await self._capture_hello_with_introspection(
            client, {"big-model": 131072, "small-model": 8192}
        )
        self.assertEqual(sorted(msg["models"]), ["big-model", "small-model"])
        self.assertEqual(msg["capabilities"]["max_context_window"], 8192)

    async def test_hello_falls_back_to_env_when_pinned(self):
        """Backend returns no usable context (llama-cpp /v1/models, no /props)
        but operator pinned MAX_CONTEXT_WINDOW → use the env value, no retry."""
        client, _, _ = _make_client(mining_enabled=False)
        with patch.object(constants, "MAX_CONTEXT_WINDOW", 8192), \
             patch.object(constants, "MAX_CONTEXT_WINDOW_EXPLICIT", True), \
             patch.object(
                 client,
                 "_get_models_with_context",
                 new=AsyncMock(return_value={"hermes": None}),
             ), \
             patch.object(
                 client,
                 "_get_available_models",
                 new=AsyncMock(return_value=["hermes"]),
             ):
            await client._send_hello()
        msg = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(msg["capabilities"]["max_context_window"], 8192)

    async def test_hello_refuses_silent_128k_fallback(self):
        """Backend gives nothing AND env is not pinned → refuse to HELLO
        rather than register with the historic 128000 lie. Caller loop
        will retry the connection by which time vllm/llama may answer."""
        client, _, _ = _make_client(mining_enabled=False)
        # Patch asyncio.sleep to no-op so the retry loop doesn't actually wait.
        with patch.object(constants, "MAX_CONTEXT_WINDOW_EXPLICIT", False), \
             patch.object(
                 client,
                 "_get_models_with_context",
                 new=AsyncMock(return_value={}),
             ), \
             patch("worker_client.asyncio.sleep", new=AsyncMock(return_value=None)):
            with self.assertRaises(RuntimeError) as cm:
                await client._send_hello()
        self.assertIn("Refusing HELLO", str(cm.exception))
        client.ws.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
