"""
Error handling and reconnection tests
Tests robustness under various failure conditions and network issues
"""
import pytest
import asyncio
import json
import time
import aiohttp
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from aioresponses import aioresponses

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from worker_client import BrokerWorkerClient
from components import constants


class NetworkFailureSimulator:
    """Simulate various network failure conditions"""
    
    def __init__(self):
        self.connection_failures = 0
        self.timeout_failures = 0
        self.intermittent_failures = []
        
    def should_fail_connection(self):
        """Simulate connection establishment failure"""
        if self.connection_failures > 0:
            self.connection_failures -= 1
            return True
        return False
        
    def should_timeout(self):
        """Simulate network timeout"""
        if self.timeout_failures > 0:
            self.timeout_failures -= 1
            return True
        return False
        
    def set_connection_failures(self, count):
        self.connection_failures = count
        
    def set_timeout_failures(self, count):
        self.timeout_failures = count


class TestReconnectionLogic:
    """Test exponential backoff and reconnection behavior"""
    
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'BROKER_WS_URL', 'ws://localhost:18080/v1/ws'):
            with patch.object(constants, 'X_WORKER_TOKEN', 'test-token'):
                client = BrokerWorkerClient()
                return client
                
    @pytest.fixture
    def network_sim(self):
        return NetworkFailureSimulator()
        
    @pytest.mark.asyncio
    async def test_exponential_backoff_timing(self, client, network_sim):
        """Test exponential backoff reconnection delays"""
        
        network_sim.set_connection_failures(3)  # Fail first 3 attempts
        
        reconnect_times = []
        original_sleep = asyncio.sleep
        
        async def mock_sleep(delay):
            reconnect_times.append(delay)
            await original_sleep(0.01)  # Speed up test
            
        with patch('asyncio.sleep', side_effect=mock_sleep):
            with patch.object(client, '_connect_and_run') as mock_connect:
                
                # Simulate connection failures
                async def failing_connect():
                    if network_sim.should_fail_connection():
                        raise aiohttp.ClientError("Connection failed")
                    return  # Success
                    
                mock_connect.side_effect = failing_connect
                
                # Start client (will attempt connections)
                start_task = asyncio.create_task(client.start())
                
                # Give it time to make several reconnection attempts
                await asyncio.sleep(0.1)
                
                # Stop client
                await client.stop()
                start_task.cancel()
                
                # Verify exponential backoff pattern
                assert len(reconnect_times) >= 2, f"Expected backoff delays, got: {reconnect_times}"
                
                # First delay should be 1 second
                assert reconnect_times[0] == 1
                
                # Second delay should be 2 seconds (doubled)
                if len(reconnect_times) > 1:
                    assert reconnect_times[1] == 2
                    
                # Third delay should be 4 seconds (doubled again)
                if len(reconnect_times) > 2:
                    assert reconnect_times[2] == 4
                    
    @pytest.mark.asyncio
    async def test_max_reconnect_delay_cap(self, client):
        """Test that reconnect delay is capped at maximum"""
        
        reconnect_times = []
        
        async def mock_sleep(delay):
            reconnect_times.append(delay)
            await asyncio.sleep(0.01)
            
        with patch('asyncio.sleep', side_effect=mock_sleep):
            with patch.object(client, '_connect_and_run') as mock_connect:
                
                # Always fail connection
                mock_connect.side_effect = aiohttp.ClientError("Always fails")
                
                start_task = asyncio.create_task(client.start())
                
                # Wait long enough for several failures (delays: 1, 2, 4, 8, 16, 32, 60, 60, ...)
                await asyncio.sleep(0.2)
                
                await client.stop()
                start_task.cancel()
                
                # Should eventually cap at 60 seconds
                if len(reconnect_times) > 6:
                    # After enough failures, should hit the cap
                    max_delays = [d for d in reconnect_times if d >= 60]
                    assert len(max_delays) > 0, "Should have hit 60s cap"
                    
    @pytest.mark.asyncio
    async def test_successful_reconnection_resets_delay(self, client, network_sim):
        """Test that successful connection resets backoff delay"""
        
        network_sim.set_connection_failures(2)  # Fail first 2 attempts, then succeed
        
        reconnect_times = []
        connection_attempts = 0
        
        async def mock_sleep(delay):
            reconnect_times.append(delay)
            await asyncio.sleep(0.01)
            
        with patch('asyncio.sleep', side_effect=mock_sleep):
            with patch.object(client, '_connect_and_run') as mock_connect:
                
                async def connect_with_eventual_success():
                    nonlocal connection_attempts
                    connection_attempts += 1
                    
                    if network_sim.should_fail_connection():
                        raise aiohttp.ClientError(f"Connection failed #{connection_attempts}")
                    
                    # Successful connection - simulate running briefly then disconnect
                    await asyncio.sleep(0.05)
                    raise aiohttp.ClientError("Disconnected after success")
                    
                mock_connect.side_effect = connect_with_eventual_success
                
                start_task = asyncio.create_task(client.start())
                await asyncio.sleep(0.2)  # Let it cycle through failures and success
                
                await client.stop()
                start_task.cancel()
                
                # Should have some reconnect attempts
                assert len(reconnect_times) >= 3
                
                # After successful connection, the delay should reset to 1 second
                # Look for a pattern like [1, 2, 1, ...] indicating reset
                delay_resets = []
                for i in range(1, len(reconnect_times)):
                    if reconnect_times[i] < reconnect_times[i-1]:
                        delay_resets.append(i)
                        
                assert len(delay_resets) > 0, "Delay should reset after successful connection"


class TestWebSocketErrorHandling:
    """Test WebSocket-specific error conditions"""
    
    @pytest.fixture
    def client(self):
        client = BrokerWorkerClient()
        client.session = AsyncMock()
        return client
        
    @pytest.mark.asyncio
    async def test_websocket_timeout_handling(self, client):
        """Test handling of WebSocket connection timeouts"""
        
        with patch.object(client.session, 'ws_connect') as mock_ws_connect:
            # Simulate timeout during connection
            mock_ws_connect.side_effect = asyncio.TimeoutError("Connection timed out")
            
            # This should raise the timeout error (to be caught by reconnection logic)
            with pytest.raises(asyncio.TimeoutError):
                await client._connect_and_run()
                
    @pytest.mark.asyncio
    async def test_websocket_close_handling(self, client, caplog):
        """Test handling when WebSocket connection is closed by server"""
        
        mock_ws = AsyncMock()
        mock_ws.closed = False
        
        # Simulate server closing connection
        async def mock_msg_iter():
            yield MagicMock(type=aiohttp.WSMsgType.CLOSE)
            
        mock_ws.__aiter__ = mock_msg_iter
        
        with patch.object(client.session, 'ws_connect') as mock_ws_connect:
            mock_ws_connect.return_value.__aenter__.return_value = mock_ws
            
            with caplog.at_level('INFO'):
                await client._connect_and_run()
                
            assert "WebSocket connection closed by broker" in caplog.text
            
    @pytest.mark.asyncio
    async def test_websocket_error_handling(self, client, caplog):
        """Test handling of WebSocket protocol errors"""
        
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.exception.return_value = Exception("WebSocket protocol error")
        
        async def mock_msg_iter():
            yield MagicMock(type=aiohttp.WSMsgType.ERROR)
            
        mock_ws.__aiter__ = mock_msg_iter
        
        with patch.object(client.session, 'ws_connect') as mock_ws_connect:
            mock_ws_connect.return_value.__aenter__.return_value = mock_ws
            
            with caplog.at_level('ERROR'):
                await client._connect_and_run()
                
            assert "WebSocket error" in caplog.text


class TestHTTPEndpointErrorHandling:
    """Test error handling for HTTP endpoint calls"""
    
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'HTTP_PORT', 8080):
            client = BrokerWorkerClient()
            client.session = AsyncMock()
            return client
            
    @pytest.mark.asyncio
    async def test_models_endpoint_errors(self, client, caplog):
        """Test error handling when models endpoint fails"""
        
        with aioresponses() as m:
            # Simulate various HTTP errors
            m.get(f"{client.miner_proxy_url}/v1/models", status=500)
            m.get(f"{client.miner_proxy_url}/status", status=500)
            
            with caplog.at_level('WARNING'):
                models = await client._get_available_models()
                
            # Should fallback to configured models
            assert len(models) > 0
            assert "Failed to get models from /v1/models endpoint" in caplog.text
            assert "Using fallback model configuration" in caplog.text
            
    @pytest.mark.asyncio
    async def test_status_endpoint_timeout(self, client, caplog):
        """Test timeout handling for status endpoint calls"""
        
        with aioresponses() as m:
            # Simulate timeout
            m.get(f"{client.miner_proxy_url}/status", 
                  exception=asyncio.TimeoutError("Request timed out"))
            
            with caplog.at_level('DEBUG'):
                input_tps = await client._get_input_tps()
                output_tps = await client._get_output_tps()
                
            assert input_tps == 0.0
            assert output_tps == 0.0
            assert "Failed to get input TPS" in caplog.text
            
    @pytest.mark.asyncio
    async def test_job_processing_http_errors(self, client):
        """Test error handling during job processing HTTP calls"""
        
        job_id = "job-error-test"
        
        # Mock HTTP error during chat completions request
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.text.return_value = "Internal server error"
        
        client.session.post.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        start_msg = {
            "job_id": job_id,
            "payload": {"messages": [{"role": "user", "content": "test"}]}
        }
        
        # Should handle error gracefully
        await client._handle_job_start(start_msg)
        
        # Should send ERROR message
        error_messages = []
        for call in client.ws.send_str.call_args_list:
            msg_data = json.loads(call[0][0])
            if msg_data.get("type") == "ERROR":
                error_messages.append(msg_data)
                
        assert len(error_messages) == 1
        assert error_messages[0]["job_id"] == job_id


class TestProofRequestErrorHandling:
    """Test error handling in proof request processing"""
    
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'HTTP_PORT', 8080):
            client = BrokerWorkerClient()
            client.session = AsyncMock()
            return client
            
    @pytest.mark.asyncio
    async def test_proof_request_timeout(self, client):
        """Test timeout handling in proof requests"""
        
        test_completion_id = "cmpl-timeout-test"
        
        with aioresponses() as m:
            # Simulate timeout
            m.get(f"{client.miner_proxy_url}/v1/proof/{test_completion_id}",
                  exception=asyncio.TimeoutError("Proof request timed out"))
            
        client.ws = AsyncMock()
        
        proof_request_msg = {"completion_id": test_completion_id}
        
        await client._handle_proof_request(proof_request_msg)
        
        # Should send timeout error response
        client.ws.send_str.assert_called_once()
        sent_data = json.loads(client.ws.send_str.call_args[0][0])
        
        assert sent_data["type"] == "PROOF_RESULT"
        assert sent_data["completion_id"] == test_completion_id
        assert sent_data["error"] == "proof_timeout"
        
    @pytest.mark.asyncio
    async def test_proof_request_not_ready(self, client):
        """Test handling when proof is not yet available"""
        
        test_completion_id = "cmpl-not-ready-test"
        
        with aioresponses() as m:
            # Simulate 404 (proof not ready)
            m.get(f"{client.miner_proxy_url}/v1/proof/{test_completion_id}", status=404)
            
        client.ws = AsyncMock()
        
        proof_request_msg = {"completion_id": test_completion_id}
        
        await client._handle_proof_request(proof_request_msg)
        
        # Should send specific not ready error
        client.ws.send_str.assert_called_once()
        sent_data = json.loads(client.ws.send_str.call_args[0][0])
        
        assert sent_data["type"] == "PROOF_RESULT"
        assert sent_data["completion_id"] == test_completion_id
        assert sent_data["error"] == "proof_not_ready"
        
    @pytest.mark.asyncio
    async def test_empty_proof_blob_error(self, client):
        """Test handling when proof blob is empty"""
        
        test_completion_id = "cmpl-empty-proof-test"
        
        with aioresponses() as m:
            # Return empty response
            m.get(f"{client.miner_proxy_url}/v1/proof/{test_completion_id}",
                  body=b"", status=200)
            
        client.ws = AsyncMock()
        
        proof_request_msg = {"completion_id": test_completion_id}
        
        await client._handle_proof_request(proof_request_msg)
        
        # Should send error about empty blob
        client.ws.send_str.assert_called_once()
        sent_data = json.loads(client.ws.send_str.call_args[0][0])
        
        assert sent_data["type"] == "PROOF_RESULT"
        assert sent_data["completion_id"] == test_completion_id
        assert "internal_error:Empty proof blob received" in sent_data["error"]


class TestGracefulShutdown:
    """Test graceful shutdown and cleanup"""
    
    @pytest.fixture
    def client(self):
        client = BrokerWorkerClient()
        return client
        
    @pytest.mark.asyncio
    async def test_graceful_stop_sequence(self, client, caplog):
        """Test graceful shutdown sequence"""
        
        # Mock components
        mock_heartbeat_task = AsyncMock()
        mock_heartbeat_task.done.return_value = False
        client.heartbeat_task = mock_heartbeat_task
        
        mock_ws = AsyncMock()
        mock_ws.closed = False
        client.ws = mock_ws
        
        mock_session = AsyncMock()
        client.session = mock_session
        
        with caplog.at_level('INFO'):
            await client.stop()
            
        # Verify cleanup sequence
        assert "Stopping broker worker client" in caplog.text
        assert "Broker worker client stopped" in caplog.text
        
        # Verify components were cleaned up
        mock_heartbeat_task.cancel.assert_called_once()
        mock_ws.close.assert_called_once()
        mock_session.close.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_stop_with_active_jobs(self, client):
        """Test shutdown behavior with active jobs"""
        
        # Add some active jobs
        client.active_jobs.add("job1")
        client.active_jobs.add("job2")
        
        assert len(client.active_jobs) == 2
        
        # Mock components for clean shutdown
        client.heartbeat_task = None  # No heartbeat task
        client.ws = None  # No WebSocket
        mock_session = AsyncMock()
        client.session = mock_session
        
        await client.stop()
        
        # Jobs should still be tracked (they might be in progress)
        # But client should be cleanly shut down
        assert not client.running
        mock_session.close.assert_called_once()


class TestResourceLeakPrevention:
    """Test prevention of resource leaks under error conditions"""
    
    @pytest.fixture
    def client(self):
        client = BrokerWorkerClient()
        return client
        
    @pytest.mark.asyncio
    async def test_session_cleanup_on_repeated_failures(self, client):
        """Test that sessions are properly cleaned up on repeated failures"""
        
        original_session_close = AsyncMock()
        
        connection_attempts = 0
        
        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session.close = original_session_close
            mock_session_class.return_value = mock_session
            
            # Make ws_connect always fail
            mock_session.ws_connect.side_effect = aiohttp.ClientError("Always fails")
            
            start_task = asyncio.create_task(client.start())
            
            # Let it fail a few times
            await asyncio.sleep(0.1)
            
            await client.stop()
            start_task.cancel()
            
            # Session should be created and properly closed
            mock_session_class.assert_called()
            original_session_close.assert_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])