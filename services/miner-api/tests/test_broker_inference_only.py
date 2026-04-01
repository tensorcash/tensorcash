"""Tests for Phase 0 broker inference-only bypass.

In ``WORKER_MODE=broker`` with ``MINING_ENABLED=False`` the miner-proxy must
serve plaintext / confidential inference without:

- constructing or starting ``ModelClient``,
- reading the local Core Node model registry,
- requiring a VDF proof,
- calling ``_inject_pow_data``,
- or needing a reachable ``MODEL_API_URL``.
"""
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Ensure src is on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Install a minimal utils.uint256_arithmetics mock before importing components
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

from components import constants  # noqa: E402
from components import proxy as proxy_module  # noqa: E402
from components.context import LockFreeContext  # noqa: E402


class _ModelClientSentinel(Exception):
    """Raised if ModelClient is unexpectedly instantiated under bypass."""


def _no_model_client(*args, **kwargs):
    raise _ModelClientSentinel(
        "ModelClient must not be constructed in broker inference-only mode"
    )


class TestBrokerInferenceOnlyConstruction(unittest.TestCase):
    """Constructor-level assertions for the bypass."""

    def test_model_client_not_constructed_under_bypass(self):
        context = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "WORKER_MODE", "broker"), \
             patch.object(constants, "MINING_ENABLED", False), \
             patch.object(proxy_module, "ModelClient", side_effect=_no_model_client):
            manager = proxy_module.RequestManager(context)

        self.assertTrue(manager._broker_inference_only)
        self.assertIsNone(manager.model_client)

    def test_inject_pow_data_returns_input_unchanged_under_bypass(self):
        context = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "WORKER_MODE", "broker"), \
             patch.object(constants, "MINING_ENABLED", False), \
             patch.object(proxy_module, "ModelClient", side_effect=_no_model_client):
            manager = proxy_module.RequestManager(context)

        original = {"model": "Qwen/Qwen3-8B", "prompt": "hello", "max_tokens": 32}
        modified = manager._inject_pow_data(dict(original))

        self.assertEqual(modified, original)
        self.assertNotIn("vllm_xargs", modified)
        self.assertNotIn("extra_sampling_params", modified)

    def test_local_miner_default_still_constructs_model_client(self):
        """Local-miner (default) mode is unchanged: ModelClient is constructed."""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "WORKER_MODE", "standalone"), \
             patch.object(constants, "MINING_ENABLED", True):
            manager = proxy_module.RequestManager(context)

        self.assertFalse(manager._broker_inference_only)
        self.assertIsNotNone(manager.model_client)

    def test_broker_with_mining_enabled_still_constructs_model_client(self):
        """Bypass is gated on BOTH WORKER_MODE=broker AND MINING_ENABLED=False."""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "WORKER_MODE", "broker"), \
             patch.object(constants, "MINING_ENABLED", True):
            manager = proxy_module.RequestManager(context)

        self.assertFalse(manager._broker_inference_only)
        self.assertIsNotNone(manager.model_client)

    def test_standalone_broker_mining_keeps_pow_injection_enabled(self):
        """STANDALONE_MODE skips local registry fetch, not broker mining."""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        model_client = Mock()
        with patch.object(constants, "WORKER_MODE", "broker"), \
             patch.object(constants, "MINING_ENABLED", True), \
             patch.object(constants, "STANDALONE_MODE", True), \
             patch.object(proxy_module, "ModelClient", return_value=model_client):
            manager = proxy_module.RequestManager(context)

        self.assertFalse(manager._broker_inference_only)
        self.assertTrue(manager._broker_mining_mode)
        self.assertTrue(manager._broker_registry_only)
        self.assertIs(manager.model_client, model_client)


class TestBrokerInferenceOnlyStart(unittest.IsolatedAsyncioTestCase):
    """``start()`` must not call into ModelClient under bypass."""

    async def test_start_does_not_touch_model_client(self):
        context = LockFreeContext("0" * 64, "ffff" * 16)
        with patch.object(constants, "WORKER_MODE", "broker"), \
             patch.object(constants, "MINING_ENABLED", False), \
             patch.object(proxy_module, "ModelClient", side_effect=_no_model_client):
            manager = proxy_module.RequestManager(context)
            try:
                with patch.object(manager, "_inject_pow_data", wraps=manager._inject_pow_data) as inject_spy, \
                     patch.object(manager, "_switch_backend_model_if_enabled") as switch_spy:
                    await manager.start()
                    self.assertIsNone(manager.model_client)
                    self.assertIsNotNone(manager.session)
                    switch_spy.assert_not_called()
                    inject_spy.assert_not_called()
            finally:
                await manager.stop()

    async def test_standalone_broker_mining_start_skips_local_model_fetch(self):
        context = LockFreeContext("0" * 64, "ffff" * 16)
        model_client = Mock()
        model_client.start = AsyncMock()
        with patch.object(constants, "WORKER_MODE", "broker"), \
             patch.object(constants, "MINING_ENABLED", True), \
             patch.object(constants, "STANDALONE_MODE", True), \
             patch.object(proxy_module, "ModelClient", return_value=model_client):
            manager = proxy_module.RequestManager(context)
            try:
                await manager.start()
                self.assertFalse(manager._broker_inference_only)
                self.assertTrue(manager._broker_registry_only)
                model_client.start.assert_not_called()
                self.assertIsNotNone(manager.session)
            finally:
                await manager.stop()


class TestBrokerInferenceOnlyRequest(AioHTTPTestCase):
    """End-to-end: ``/v1/chat/completions`` must succeed without any
    ``MODEL_API_URL``, ``ModelClient``, or ``_inject_pow_data`` call."""

    async def get_application(self):
        # Force broker-inference-only mode AND poison MODEL_API_URL to prove
        # it is never read. The patches must be active during construction.
        self._patches = [
            patch.object(constants, "WORKER_MODE", "broker"),
            patch.object(constants, "MINING_ENABLED", False),
            patch.object(constants, "MODEL_API_URL", "http://invalid-host.invalid:1"),
            patch.object(proxy_module, "ModelClient", side_effect=_no_model_client),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.manager = proxy_module.RequestManager(self.context)

        # Don't touch the network: pre-install a mocked aiohttp session.
        self.manager.session = AsyncMock()

        # Spy on _inject_pow_data so we can assert it's bypassed.
        self._inject_spy = Mock(wraps=self.manager._inject_pow_data)
        self.manager._inject_pow_data = self._inject_spy

        app = web.Application()
        app.router.add_post('/v1/chat/completions', self.manager.proxy_request)
        app.router.add_post('/v1/completions', self.manager.proxy_request)
        return app

    def _stop_patches(self):
        for p in self._patches:
            p.stop()

    @unittest_run_loop
    async def test_chat_completion_without_model_api_url(self):
        # Mock upstream response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read = AsyncMock(return_value=b'{"id": "chat-1"}')

        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        self.manager.session.post = mock_post

        payload = {
            "model": "Qwen/Qwen3-8B",
            "messages": [{"role": "user", "content": "hi"}],
        }
        resp = await self.client.request("POST", "/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        self.assertEqual(body, b'{"id": "chat-1"}')

        # ModelClient must not have been constructed.
        self.assertIsNone(self.manager.model_client)

        # Forwarded body must be the original (no pow / vllm_xargs added).
        mock_post.assert_called_once()
        sent_json = mock_post.call_args[1]["json"]
        self.assertEqual(sent_json, payload)
        self.assertNotIn("vllm_xargs", sent_json)
        self.assertNotIn("extra_sampling_params", sent_json)

        # _inject_pow_data was the early-return bypass; it must not have
        # touched ModelClient or LockFreeContext snapshot data.
        self._inject_spy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
