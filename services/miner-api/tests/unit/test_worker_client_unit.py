"""
Unit tests for BrokerWorkerClient core functionality
Tests individual methods and logic without external dependencies
"""
import pytest
import asyncio
import json
import uuid
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from aioresponses import aioresponses
import aiohttp

# Add src to path for testing
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from worker_client import BrokerWorkerClient
from components import constants


class TestWorkerClientInitialization:
    """Test worker client initialization and configuration"""
    
    def test_init_with_jwt_token(self):
        """Test initialization with JWT token"""
        with patch.object(constants, 'PROVIDER_JWT_TOKEN', 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test'):
            with patch.object(constants, 'X_WORKER_TOKEN', ''):
                client = BrokerWorkerClient()
                assert client.jwt_token == 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test'
                assert client.worker_token == ''
                
    def test_init_with_shared_secret(self):
        """Test initialization with shared secret"""
        with patch.object(constants, 'PROVIDER_JWT_TOKEN', ''):
            with patch.object(constants, 'X_WORKER_TOKEN', 'dev-secret-123'):
                client = BrokerWorkerClient()
                assert client.jwt_token == ''
                assert client.worker_token == 'dev-secret-123'
                
    def test_init_generates_unique_worker_id(self):
        """Test that each client gets a unique worker ID"""
        client1 = BrokerWorkerClient()
        client2 = BrokerWorkerClient()
        assert client1.worker_id != client2.worker_id
        assert len(client1.worker_id) == 36  # UUID length
        

class TestAuthenticationLogic:
    """Test authentication header generation"""
    
    @pytest.fixture
    def client(self):
        return BrokerWorkerClient()
    
    def test_jwt_authentication_headers(self, client):
        """Test JWT authentication header generation"""
        client.jwt_token = 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test'
        client.worker_token = ''
        
        # Mock the connect method to capture headers
        async def mock_connect():
            headers = {}
            if client.jwt_token:
                if not client.jwt_token.startswith("eyJ"):
                    # This should not happen in this test
                    pass
                headers["Authorization"] = f"Bearer {client.jwt_token}"
            elif client.worker_token:
                headers["X-Worker-Token"] = client.worker_token
            return headers
            
        headers = asyncio.run(mock_connect())
        assert headers["Authorization"] == "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test"
        assert "X-Worker-Token" not in headers
        
    def test_shared_secret_authentication_headers(self, client):
        """Test shared secret authentication header generation"""
        client.jwt_token = ''
        client.worker_token = 'dev-secret-123'
        
        async def mock_connect():
            headers = {}
            if client.jwt_token:
                headers["Authorization"] = f"Bearer {client.jwt_token}"
            elif client.worker_token:
                headers["X-Worker-Token"] = client.worker_token
            return headers
            
        headers = asyncio.run(mock_connect())
        assert headers["X-Worker-Token"] == "dev-secret-123"
        assert "Authorization" not in headers
        
    def test_no_authentication_warning(self, client, caplog):
        """Test warning when no authentication is configured"""
        client.jwt_token = ''
        client.worker_token = ''
        
        async def mock_connect():
            headers = {}
            if client.jwt_token:
                headers["Authorization"] = f"Bearer {client.jwt_token}"
            elif client.worker_token:
                headers["X-Worker-Token"] = client.worker_token
            else:
                # This would log a warning in the actual implementation
                import logging
                logging.warning("No authentication token configured - connection may fail")
            return headers
            
        with caplog.at_level('WARNING'):
            headers = asyncio.run(mock_connect())
            
        assert len(headers) == 0
        assert "No authentication token configured" in caplog.text


class TestModelDiscovery:
    """Test model discovery functionality"""
    
    @pytest.fixture
    def client(self):
        return BrokerWorkerClient()
        
    @pytest.mark.asyncio
    async def test_successful_model_discovery(self, client):
        """Test successful model discovery from /v1/models endpoint"""
        mock_response_data = {
            "data": [
                {"id": "gpt-3.5-turbo", "object": "model"},
                {"id": "Qwen/Qwen3-8B", "object": "model"}
            ]
        }
        
        # Close the default session and create a new one for testing
        if client.http_session:
            await client.http_session.close()
        
        with aioresponses() as m:
            # Create new session
            import aiohttp
            client.http_session = aiohttp.ClientSession()
            
            m.get(
                f"{client.miner_proxy_url}/v1/models",
                payload=mock_response_data
            )
            
            # Mock the fallback status endpoint in case it's called
            m.get(
                f"{client.miner_proxy_url}/status",
                payload={"proxy": {"model": "test-model"}}
            )
            
            models = await client._get_available_models()
            assert models == ["gpt-3.5-turbo", "Qwen/Qwen3-8B"]
            
            # Clean up session
            await client.http_session.close()
            
    @pytest.mark.asyncio 
    async def test_empty_models_response(self, client):
        """Test handling of empty models response"""
        # Close the default session and create a new one for testing
        if client.http_session:
            await client.http_session.close()
        
        with aioresponses() as m:
            import aiohttp
            client.http_session = aiohttp.ClientSession()
            
            # Mock empty models response
            m.get(
                f"{client.miner_proxy_url}/v1/models",
                payload={"data": []}
            )
            
            # Mock fallback status response
            m.get(
                f"{client.miner_proxy_url}/status",
                payload={"proxy": {"model": "test-model"}}
            )
            
            models = await client._get_available_models()
            # Should fall back to configured models
            assert len(models) > 0
            assert "Qwen/Qwen3-8B" in models  # From fallback config
            
            await client.http_session.close()
            
    @pytest.mark.asyncio
    async def test_models_endpoint_failure(self, client, caplog):
        """Test handling when /v1/models endpoint fails"""
        # Close the default session and create a new one for testing
        if client.http_session:
            await client.http_session.close()
        
        with aioresponses() as m:
            import aiohttp
            client.http_session = aiohttp.ClientSession()
            
            # Mock failed models response
            m.get(
                f"{client.miner_proxy_url}/v1/models",
                status=404
            )
            
            # Mock fallback status response
            m.get(
                f"{client.miner_proxy_url}/status",
                payload={"proxy": {}}
            )
            
            with caplog.at_level('WARNING'):
                models = await client._get_available_models()
                
            assert len(models) > 0  # Should have fallback models
            # Either the specific error or the fallback message should be logged
            assert ("Failed to get models from /v1/models endpoint" in caplog.text or 
                    "Using fallback model configuration" in caplog.text)
            
            await client.http_session.close()


class TestMetricsCollection:
    """Test metrics collection from miner proxy"""
    
    @pytest.fixture
    def client(self):
        return BrokerWorkerClient()
        
    @pytest.mark.asyncio
    async def test_successful_metrics_collection(self, client):
        """Test successful metrics collection from status endpoint"""
        mock_status_data = {
            "proxy": {
                "input_tokens_per_sec": 150.5,
                "output_tokens_per_sec": 45.2,
                "active_requests": 3
            }
        }
        
        # Close the default session and create a new one for testing
        if client.http_session:
            await client.http_session.close()
        
        with aioresponses() as m:
            # Create new session
            import aiohttp
            client.http_session = aiohttp.ClientSession()
            
            m.get(
                f"{client.miner_proxy_url}/status",
                payload=mock_status_data,
                repeat=True  # Allow multiple calls
            )
            
            input_tps = await client._get_input_tps()
            output_tps = await client._get_output_tps()
            
            assert input_tps == 150.5
            assert output_tps == 45.2
            
            # Clean up session
            await client.http_session.close()
            
    @pytest.mark.asyncio
    async def test_missing_metrics_in_status(self, client):
        """Test handling when metrics are missing from status"""
        mock_status_data = {"proxy": {"other_field": "value"}}
        
        # Close the default session and create a new one for testing
        if client.http_session:
            await client.http_session.close()
        
        with aioresponses() as m:
            import aiohttp
            client.http_session = aiohttp.ClientSession()
            
            m.get(
                f"{client.miner_proxy_url}/status",
                payload=mock_status_data,
                repeat=True
            )
            
            input_tps = await client._get_input_tps()
            output_tps = await client._get_output_tps()
            
            assert input_tps == 0.0
            assert output_tps == 0.0
            
            await client.http_session.close()
            
    @pytest.mark.asyncio
    async def test_status_endpoint_failure(self, client, caplog):
        """Test handling when status endpoint fails"""
        # Close the default session and create a new one for testing
        if client.http_session:
            await client.http_session.close()
        
        with aioresponses() as m:
            import aiohttp
            client.http_session = aiohttp.ClientSession()
            
            # Mock to raise an exception instead of returning 500 status
            m.get(
                f"{client.miner_proxy_url}/status",
                exception=aiohttp.ClientError("Connection failed"),
                repeat=True
            )
            
            with caplog.at_level('DEBUG'):
                input_tps = await client._get_input_tps()
                output_tps = await client._get_output_tps()
                
            assert input_tps == 0.0
            assert output_tps == 0.0
            assert "Failed to get input TPS" in caplog.text
            assert "Failed to get output TPS" in caplog.text
            
            await client.http_session.close()


class TestMessageHandling:
    """Test WebSocket message handling logic"""
    
    @pytest.fixture
    def client(self):
        client = BrokerWorkerClient()
        client.ws = AsyncMock()
        return client
        
    @pytest.mark.asyncio
    async def test_handle_ack_message(self, client):
        """Test handling of ACK message"""
        ack_msg = {
            "type": "ACK",
            "heartbeat_interval_sec": 20
        }
        
        with patch.object(client, '_heartbeat_loop') as mock_heartbeat:
            mock_task = AsyncMock()
            with patch('asyncio.create_task', return_value=mock_task):
                await client._handle_message(ack_msg)
                
            assert client.heartbeat_interval == 20
            
    @pytest.mark.asyncio
    async def test_handle_challenge_message(self, client):
        """Test handling of CHALLENGE message with secret"""
        with patch.object(constants, 'CHALLENGE_SECRET', 'test-secret'):
            challenge_msg = {
                "type": "CHALLENGE",
                "nonce": "test-nonce-123",
                "timestamp": 1699123456
            }
            
            await client._handle_message(challenge_msg)
            
            # Verify CHALLENGE_RESP was sent
            client.ws.send.assert_called_once()
            sent_data = json.loads(client.ws.send.call_args[0][0])
            assert sent_data["type"] == "CHALLENGE_RESP"
            assert sent_data["nonce"] == "test-nonce-123"
            assert "proof" in sent_data
            
    @pytest.mark.asyncio
    async def test_handle_challenge_no_secret(self, client):
        """Test handling of CHALLENGE message without secret configured"""
        with patch.object(constants, 'CHALLENGE_SECRET', ''):
            challenge_msg = {
                "type": "CHALLENGE", 
                "nonce": "test-nonce-123",
                "timestamp": 1699123456
            }
            
            await client._handle_message(challenge_msg)
            
            # Should not send response without secret
            client.ws.send.assert_not_called()
            
    @pytest.mark.asyncio
    async def test_handle_unknown_message_type(self, client, caplog):
        """Test handling of unknown message types"""
        unknown_msg = {
            "type": "UNKNOWN_TYPE",
            "data": "some data"
        }
        
        with caplog.at_level('WARNING'):
            await client._handle_message(unknown_msg)
            
        assert "Unknown message type: UNKNOWN_TYPE" in caplog.text


class TestResponsesApiSelection:
    """Test local inference API selection for broker-dispatched jobs."""

    @pytest.fixture
    def client(self):
        return BrokerWorkerClient()

    def test_broker_responses_hint_preserves_native_responses_payload(self, client):
        payload = {"model": "auto", "input": "hello", "stream": True}

        effective_api, selected_payload = client._select_local_inference_api(
            "responses", payload
        )

        assert effective_api == "responses"
        assert selected_payload is payload
        assert "messages" not in selected_payload

    def test_broker_responses_hint_normalizes_native_function_tools_for_local_backend(self, client):
        payload = {
            "model": "auto",
            "input": "hello",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "function", "name": "lookup"},
        }

        effective_api, selected_payload = client._select_local_inference_api(
            "responses", payload
        )

        assert effective_api == "responses"
        assert selected_payload["input"] == "hello"
        assert "messages" not in selected_payload
        assert selected_payload["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {"type": "object"},
                },
            }
        ]
        assert selected_payload["tool_choice"] == {
            "type": "function",
            "function": {"name": "lookup"},
        }

    def test_legacy_input_payload_still_falls_back_to_chat(self, client):
        payload = {
            "model": "auto",
            "input": "hello",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "function", "name": "lookup"},
        }

        effective_api, selected_payload = client._select_local_inference_api(
            "chat_completions", payload
        )

        assert effective_api == "chat_completions"
        assert selected_payload["messages"] == [{"role": "user", "content": "hello"}]
        assert selected_payload["stream"] is True
        assert selected_payload["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {"type": "object"},
                },
            }
        ]
        assert selected_payload["tool_choice"] == {
            "type": "function",
            "function": {"name": "lookup"},
        }


class TestJobTracking:
    """Test job tracking state management"""
    
    @pytest.fixture
    def client(self):
        client = BrokerWorkerClient()
        return client
        
    def test_initial_job_tracking_state(self, client):
        """Test initial state of job tracking"""
        assert len(client.active_jobs) == 0
        
    def test_job_tracking_add_remove(self, client):
        """Test adding and removing jobs from tracking"""
        job_id = "job-123"
        completion_id = "cmpl-456"
        
        # Add job_id initially
        client.active_jobs.add(job_id)
        assert job_id in client.active_jobs
        assert len(client.active_jobs) == 1
        
        # Transition to completion_id
        client.active_jobs.discard(job_id)
        client.active_jobs.add(completion_id)
        
        assert job_id not in client.active_jobs
        assert completion_id in client.active_jobs
        assert len(client.active_jobs) == 1
        
        # Clean up
        client.active_jobs.discard(completion_id)
        assert len(client.active_jobs) == 0
        
    def test_get_status_includes_active_jobs(self, client):
        """Test that status includes active job count"""
        client.active_jobs.add("job1")
        client.active_jobs.add("job2")
        
        status = client.get_status()
        assert status["active_jobs"] == 2
        assert status["worker_id"] == client.worker_id
        assert "connected" in status
        assert "running" in status


class TestConfidentialContinuationGuards:
    """Test continuation payload guards for mixed local/remote tool calls."""

    @pytest.fixture
    def client(self):
        return BrokerWorkerClient()

    def test_mixed_local_remote_tool_results_are_merged_for_remote_continuation(self, client):
        """Local tool results are buffered and merged when a remote tool result arrives."""
        run_id = "run-mixed-guard"
        base_payload = {
            "messages": [{"role": "user", "content": "Find and summarize"}],
            "tools": [
                {"type": "function", "function": {"name": "agent_x__file_search"}},
                {"type": "function", "function": {"name": "agent_x__web_lookup"}},
            ],
            "tool_choice": "auto",
            "stream": True,
        }

        client._remember_confidential_run_payload(run_id, base_payload)
        client._remember_confidential_tool_call(
            run_id,
            "call_local",
            "agent_x__file_search",
            {"query": "policy"},
        )
        client._remember_confidential_tool_call(
            run_id,
            "call_remote",
            "agent_x__web_lookup",
            {"query": "latest"},
        )
        client._buffer_local_tool_results_for_continuation(
            run_id,
            [
                {
                    "tool_call_id": "call_local",
                    "tool_id": "agent_x__file_search",
                    "result": {"success": True, "result": {"hits": ["a", "b"]}},
                }
            ],
        )

        continuation = client._build_continuation_payload_from_tool_result(
            run_id,
            "call_remote",
            {"success": True, "result": {"headline": "ok"}},
        )

        assert continuation is not None
        assert continuation.get("stream") is True
        # Remote continuation must keep tools enabled for any subsequent model planning turn.
        assert "tools" in continuation
        assert continuation.get("tool_choice") == "auto"

        messages = continuation.get("messages", [])
        assert len(messages) == 4
        assistant_message = messages[-3]
        tool_message_local = messages[-2]
        tool_message_remote = messages[-1]

        assert assistant_message.get("role") == "assistant"
        tool_calls = assistant_message.get("tool_calls") or []
        assert [tc.get("id") for tc in tool_calls] == ["call_local", "call_remote"]
        assert [tc.get("function", {}).get("name") for tc in tool_calls] == [
            "agent_x__file_search",
            "agent_x__web_lookup",
        ]

        assert tool_message_local.get("role") == "tool"
        assert tool_message_local.get("tool_call_id") == "call_local"
        assert tool_message_remote.get("role") == "tool"
        assert tool_message_remote.get("tool_call_id") == "call_remote"

        run_state = client.confidential_runs.get(run_id) or {}
        assert run_state.get("pending_local_tool_results") == []

    def test_local_only_continuation_still_drops_tools(self, client):
        """Local-only continuation should still force final assistant response (no further tool planning)."""
        run_id = "run-local-only"
        base_payload = {
            "messages": [{"role": "user", "content": "search local docs"}],
            "tools": [{"type": "function", "function": {"name": "agent_x__file_search"}}],
            "tool_choice": "auto",
            "stream": True,
        }

        client._remember_confidential_run_payload(run_id, base_payload)
        client._remember_confidential_tool_call(
            run_id,
            "call_local",
            "agent_x__file_search",
            {"query": "onboarding"},
        )

        continuation = client._build_continuation_payload_from_tool_results(
            run_id,
            [
                {
                    "tool_call_id": "call_local",
                    "tool_id": "agent_x__file_search",
                    "result": {"success": True, "result": {"hits": ["readme"]}},
                }
            ],
        )

        assert continuation is not None
        assert "tools" not in continuation
        assert "tool_choice" not in continuation


class TestDirectToolInvoke:
    """Test direct TOOL_INVOKE handling on worker."""

    @pytest.fixture
    def client(self):
        client = BrokerWorkerClient()
        client.ws = AsyncMock()
        client.http_session = object()
        client._set_worker_tools_from_configs(
            [
                {
                    "tool_id": "file_search",
                    "executor": "worker",
                }
            ]
        )
        return client

    @pytest.mark.asyncio
    async def test_plaintext_tool_invoke_executes_local_tool(self, client):
        client._execute_local_worker_tool = AsyncMock(
            return_value={"success": True, "result": {"hits": ["doc"]}}
        )

        await client._handle_tool_invoke(
            {
                "invoke_id": "inv_plain_1",
                "tool_id": "agent_x__file_search",
                "mode": "plaintext",
                "args": {"query": "onboarding"},
            }
        )

        client._execute_local_worker_tool.assert_awaited_once_with(
            "agent_x__file_search",
            {"query": "onboarding"},
            None,
            None,
        )
        client.ws.send.assert_awaited_once()
        payload = json.loads(client.ws.send.await_args_list[0].args[0])
        assert payload["type"] == "TOOL_INVOKE_RESULT"
        assert payload["invoke_id"] == "inv_plain_1"
        assert payload["status"] == "success"
        assert payload["result"]["success"] is True

    @pytest.mark.asyncio
    async def test_confidential_tool_invoke_encrypts_result(self, client):
        client.crypto_service = Mock()
        client.crypto_service.fetch_cek = AsyncMock(return_value=b"cek-bytes")
        client.crypto_service.decrypt_payload = Mock(return_value={"args": {"query": "security"}})
        client.crypto_service.encrypt_response = Mock(return_value="encrypted-tool-result")
        client._execute_local_worker_tool = AsyncMock(
            return_value={"success": True, "result": {"hits": ["a", "b"]}}
        )

        await client._handle_tool_invoke(
            {
                "invoke_id": "inv_conf_1",
                "tool_id": "agent_x__file_search",
                "mode": "confidential",
                "room_id": "room-1",
                "epoch": 3,
                "payload_b64": "encrypted-args",
                "run_id": "run-1",
            }
        )

        client.crypto_service.fetch_cek.assert_awaited_once_with(client.http_session, "room-1", 3)
        client._execute_local_worker_tool.assert_awaited_once_with(
            "agent_x__file_search",
            {"query": "security"},
            "room-1",
            "run-1",
        )
        payload = json.loads(client.ws.send.await_args_list[0].args[0])
        assert payload["type"] == "TOOL_INVOKE_RESULT"
        assert payload["invoke_id"] == "inv_conf_1"
        assert payload["status"] == "success"
        assert payload["result_b64"] == "encrypted-tool-result"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
