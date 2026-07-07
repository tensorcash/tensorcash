"""Unit tests for RequestManager proxy component"""
import unittest
import asyncio
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
import sys
import os
import types

# Ensure src is on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Install a minimal utils.uint256_arithmetics mock before importing components
if "utils.uint256_arithmetics" not in sys.modules:
    utils_pkg = types.ModuleType("utils")
    uint256_mod = types.ModuleType("utils.uint256_arithmetics")
    def set_compact(x):
        return x
    def get_compact(x):
        return x
    def adjust_nbits_by_multiplier(bits, mult, default):
        return {"target_bytes": b"\xff" * 32, "nbits": 0x1d00ffff}
    uint256_mod.set_compact = set_compact
    uint256_mod.get_compact = get_compact
    uint256_mod.adjust_nbits_by_multiplier = adjust_nbits_by_multiplier
    sys.modules["utils"] = utils_pkg
    sys.modules["utils.uint256_arithmetics"] = uint256_mod

from components.proxy import RequestManager
from components.context import LockFreeContext
from components.model_synch import ModelClient
from components import constants
from components.constants import ModelConfig


class TestRequestManager(AioHTTPTestCase):
    """Test cases for RequestManager HTTP proxy"""
    
    async def get_application(self):
        """Create test application"""
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.manager = RequestManager(self.context)
        
        # Mock the model client
        self.manager.model_client = Mock(spec=ModelClient)
        self.manager.model_client._initialized = True
        self.manager.model_client.models_by_name = {
            "test-model": [ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123",
                target_adj="7fff" * 16
            )]
        }

        # Create app
        app = web.Application()
        app.router.add_post('/v1/completions', self.manager.proxy_request)
        app.router.add_post('/v1/chat/completions', self.manager.proxy_request)
        app.router.add_post('/v1/embeddings', self.manager.proxy_request)
        app.router.add_post('/v1/responses', self.manager.proxy_request)
        app.router.add_get('/v1/responses/{response_id}', self.manager.proxy_request)
        app.router.add_post('/v1/responses/{response_id}/cancel', self.manager.proxy_request)
        app.router.add_get('/v1/models', self.manager.proxy_request)
        
        return app
    
    async def setUpAsync(self):
        """Async setup"""
        await super().setUpAsync()
        
        # Mock the session
        self.manager.session = AsyncMock()
        self.manager.active_requests = {}
    
    @unittest_run_loop
    async def test_inject_pow_data(self):
        """Test PoW data injection"""
        # Update context with test data
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=42
        )
        self.context.update_vdf("vdf_proof_data", 1000000)
        
        # Mock get_model_config
        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123",
                target_adj="7fff" * 16
            )
            
            # Test data injection
            data = {
                "model": "test-model",
                "prompt": "test prompt",
                "max_tokens": 256
            }
            
            result = self.manager._inject_pow_data(data)
            
            # Verify injected data (USE_VLLM_XARGS=true by default)
            pow_key = "vllm_xargs" if constants.USE_VLLM_XARGS else "extra_sampling_params"
            self.assertIn(pow_key, result)
            self.assertIn("pow", result[pow_key])

            pow_data = result[pow_key]["pow"]
            self.assertEqual(pow_data["block_hash"], "test_block_hash")
            self.assertEqual(pow_data["vdf"], "vdf_proof_data")
            self.assertEqual(pow_data["tick"], 1000000)
            self.assertEqual(pow_data["request_id"], 42)
            self.assertEqual(pow_data["ipfs_cid"], "Qm123")
            self.assertEqual(pow_data["difficulty"], 1000000)
            
            # Verify sampling params were added
            self.assertIn("top_k", result)
            self.assertIn("top_p", result)
            self.assertIn("temperature", result)
    
    @unittest_run_loop
    async def test_handle_completion_request(self):
        """Test completion request handling"""
        # Initialise VDF so the guard in _inject_pow_data does not reject
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=42
        )
        self.context.update_vdf("vdf_proof_data", 1000000)

        # Mock upstream response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read = AsyncMock(return_value=b'{"result": "test"}')
        
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        
        self.manager.session.post = mock_post
        
        # Mock get_model_config
        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )
            
            # Make request
            resp = await self.client.request(
                "POST", "/v1/completions",
                json={
                    "model": "test-model",
                    "prompt": "test",
                    "max_tokens": 100
                }
            )
            
            self.assertEqual(resp.status, 200)
            body = await resp.read()
            self.assertEqual(body, b'{"result": "test"}')
            
            # Verify upstream was called with injected data
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            pow_key = "vllm_xargs" if constants.USE_VLLM_XARGS else "extra_sampling_params"
            self.assertIn(pow_key, call_args[1]["json"])

    @unittest_run_loop
    async def test_inject_pow_data_llama_cpp_uses_extra_sampling_params(self):
        """llama.cpp mode should default to the legacy PoW payload field."""
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=77
        )
        self.context.update_vdf("vdf_proof_data", 1234)

        with patch('components.constants.get_model_config') as mock_get_config, \
             patch.object(constants, "LLAMA_CPP", True), \
             patch.object(constants, "USE_VLLM_XARGS", False):
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )

            result = self.manager._inject_pow_data(
                {
                    "model": "test-model",
                    "prompt": "test prompt",
                    "max_tokens": 256,
                }
            )

            self.assertIn("extra_sampling_params", result)
            self.assertIn("pow", result["extra_sampling_params"])
            pow_data = result["extra_sampling_params"]["pow"]
            self.assertEqual(pow_data["model_identifier"], "test-model@commit123")
            self.assertEqual(pow_data["compute_precision"], "bf16")
    
    @unittest_run_loop
    async def test_streaming_response(self):
        """Test streaming response handling"""
        # Initialise VDF so the guard in _inject_pow_data does not reject
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=42
        )
        self.context.update_vdf("vdf_proof_data", 1000000)

        # Mock streaming response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        
        # Mock content iterator
        async def mock_iter():
            yield b"data: chunk1\n\n"
            yield b"data: chunk2\n\n"
        
        mock_response.content.iter_chunked = Mock(return_value=mock_iter())
        
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        
        self.manager.session.post = mock_post
        
        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )
            
            # Make streaming request
            resp = await self.client.request(
                "POST", "/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": True
                }
            )
            
            self.assertEqual(resp.status, 200)
            
            # Read streamed chunks
            chunks = []
            async for chunk in resp.content:
                chunks.append(chunk)
            # Some aiohttp versions split SSE writes; accept >=2
            self.assertGreaterEqual(len(chunks), 2)

    @unittest_run_loop
    async def test_embeddings_passthrough(self):
        """Embeddings should pass through without PoW injection."""
        # Mock upstream response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read = AsyncMock(return_value=b'{"object": "list", "data": []}')

        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        self.manager.session.post = mock_post

        payload = {"model": "test-model", "input": "hello"}
        resp = await self.client.request("POST", "/v1/embeddings", json=payload)
        self.assertEqual(resp.status, 200)

        # Ensure upstream received the same payload (no PoW injection)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        sent_json = call_args[1]["json"]
        self.assertEqual(sent_json, payload)

    @unittest_run_loop
    async def test_responses_get_bad_gateway_on_client_error(self):
        """GET /v1/responses/{id} mapping to 502 on upstream client error."""
        import aiohttp
        async def raise_client_error(*args, **kwargs):
            raise aiohttp.ClientError("bad id")
        self.manager.session.get = raise_client_error
        resp = await self.client.request("GET", "/v1/responses/notfound")
        self.assertEqual(resp.status, 502)

    @unittest_run_loop
    async def test_responses_injection(self):
        """Responses API should get PoW injection by default (non-streaming)."""
        # Prepare context
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=7
        )
        self.context.update_vdf("vdf_proof_data", 123)

        # Mock upstream response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read = AsyncMock(return_value=b'{"id": "resp_123"}')
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        self.manager.session.post = mock_post

        # Mock get_model_config
        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )

            payload = {"model": "test-model", "input": "hello world"}
            resp = await self.client.request("POST", "/v1/responses", json=payload)
            self.assertEqual(resp.status, 200)

            # Verify PoW injection occurred
            mock_post.assert_called_once()
            sent = mock_post.call_args[1]["json"]
            pow_key = "vllm_xargs" if constants.USE_VLLM_XARGS else "extra_sampling_params"
            self.assertIn(pow_key, sent)
            self.assertIn("pow", sent[pow_key])

    @unittest_run_loop
    async def test_responses_streaming(self):
        """Responses API should support streaming when stream=true."""
        # Initialise VDF so the guard in _inject_pow_data does not reject
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=42
        )
        self.context.update_vdf("vdf_proof_data", 1000000)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"content-type": "text/event-stream"}

        async def mock_iter():
            yield b"data: r1\n\n"
            yield b"data: r2\n\n"

        mock_response.content.iter_chunked = Mock(return_value=mock_iter())
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        self.manager.session.post = mock_post

        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )

            payload = {"model": "test-model", "input": "abc", "stream": True}
            resp = await self.client.request("POST", "/v1/responses", json=payload)
            self.assertEqual(resp.status, 200)

            chunks = []
            async for chunk in resp.content:
                chunks.append(chunk)
            # Some aiohttp versions split SSE writes; accept >=2
            self.assertGreaterEqual(len(chunks), 2)

    @unittest_run_loop
    async def test_stream_interruption_is_handled(self):
        """Simulate upstream streaming interruption and ensure clean termination."""
        # Initialise VDF so the guard in _inject_pow_data does not reject
        self.context.update_mining(
            block_hash="test_block_hash",
            header_prefix="0" * 152,
            target="ffff" * 16,
            request_id=42
        )
        self.context.update_vdf("vdf_proof_data", 1000000)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        async def broken_iter():
            yield b"data: first\n\n"
            raise Exception("upstream stream aborted")
        mock_response.content.iter_chunked = Mock(return_value=broken_iter())
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        self.manager.session.post = mock_post
        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )
            resp = await self.client.request(
                "POST", "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
            )
            self.assertEqual(resp.status, 200)
            chunks = []
            async for c in resp.content:
                chunks.append(c)
            self.assertGreaterEqual(len(chunks), 1)
    
    @unittest_run_loop
    async def test_error_handling(self):
        """Test error handling in proxy"""
        # Test invalid JSON
        resp = await self.client.request(
            "POST", "/v1/completions",
            data="invalid json"
        )
        self.assertEqual(resp.status, 400)
        
        # Test model client not initialized
        self.manager.model_client._initialized = False
        resp = await self.client.request(
            "POST", "/v1/completions",
            json={"model": "test", "prompt": "test"}
        )
        self.assertEqual(resp.status, 503)
        self.manager.model_client._initialized = True
        
        # Test upstream error
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.headers = {}
        mock_response.read = AsyncMock(return_value=b'{"error": "upstream error"}')
        
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        
        self.manager.session.post = mock_post
        
        with patch('components.constants.get_model_config') as mock_get_config:
            mock_get_config.return_value = ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )
            
            resp = await self.client.request(
                "POST", "/v1/completions",
                json={"model": "test-model", "prompt": "test"}
            )
            self.assertEqual(resp.status, 500)
    
    def test_update_header_prefix_bits(self):
        """Test header prefix bits update"""
        # 76-byte header as hex (152 chars)
        original_prefix = "01000000" + "0" * 144  # version + rest
        new_bits = 0x1d00ffff
        
        result = RequestManager.update_header_prefix_bits(original_prefix, new_bits)
        
        # Verify length unchanged
        self.assertEqual(len(result), 152)
        
        # Verify bits were updated (bytes 72-75)
        header_bytes = bytes.fromhex(result)
        import struct
        extracted_bits = struct.unpack('<I', header_bytes[72:76])[0]
        self.assertEqual(extracted_bits, new_bits)
    
    def test_validate_sampling_params(self):
        """Test sampling parameter validation and bounds"""
        test_cases = [
            # (input, expected_top_k, expected_top_p, expected_temp)
            ({}, 50, 1.0, 1.0),  # Defaults
            ({"top_k": 5, "top_p": 0.1, "temperature": 0.25}, 5, 0.1, 0.25),  # Min endpoints
            ({"top_k": 50, "top_p": 1.0, "temperature": 2.0}, 50, 1.0, 2.0),  # Max endpoints
            ({"top_k": 0, "top_p": 0.0, "temperature": 0.0}, 5, 0.1, 0.25),  # Below bounds
            ({"top_k": 1000, "top_p": 2.0, "temperature": 3.0}, 50, 1.0, 2.0),  # Above bounds
        ]
        
        for data, exp_k, exp_p, exp_t in test_cases:
            result = self.manager._validate_and_rebound_sampling_params(data.copy())
            self.assertEqual(result["top_k"], exp_k)
            self.assertEqual(result["top_p"], exp_p)
            self.assertEqual(result["temperature"], exp_t)


class TestDummyRequestGeneration(unittest.TestCase):
    """Test dummy request generation"""
    
    def setUp(self):
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.manager = RequestManager(self.context)
        
        # Mock components
        self.manager.session = AsyncMock()
        self.manager.model_client = Mock()
        self.manager.model_client.models_by_name = {
            "Qwen/Qwen3-8B": [ModelConfig(
                model_hash="hash",
                model_name="Qwen/Qwen3-8B",
                model_commit="commit",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )]
        }

class TestPriorityResponses(AioHTTPTestCase):
    async def get_application(self):
        from components.proxy_with_priority import PriorityRequestManager
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.manager = PriorityRequestManager(self.context)
        # Mock model client readiness
        self.manager.model_client = Mock(spec=ModelClient)
        self.manager.model_client._initialized = True
        self.manager.model_client.models_by_name = {"test-model": [ModelConfig(
            model_hash="h", model_name="test-model", model_commit="c", difficulty=1000000, ipfs_cid="Qm"
        )]}
        app = web.Application()
        app.router.add_post('/v1/responses', self.manager.proxy_request)
        return app

    async def setUpAsync(self):
        await super().setUpAsync()
        # Force capacity denial
        async def deny(*args, **kwargs):
            return ("ext-1", False)
        self.manager.priority_manager.register_external_request = AsyncMock(side_effect=deny)

    @unittest_run_loop
    async def test_capacity_limit_returns_503(self):
        resp = await self.client.request("POST", "/v1/responses", json={"model": "test-model", "input": "hi"})
        self.assertEqual(resp.status, 503)


if __name__ == '__main__':
    unittest.main()
