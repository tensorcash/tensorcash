"""
Protocol integration tests for BrokerWorkerClient WebSocket communication
Tests the complete WebSocket protocol flow with mock broker server
"""
import pytest
import asyncio
import json
import websockets
import threading
import time
from unittest.mock import Mock, AsyncMock, patch
from aioresponses import aioresponses

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from worker_client import BrokerWorkerClient
from components import constants


class MockBrokerServer:
    """Mock WebSocket broker server for testing protocol compliance"""
    
    def __init__(self, host="localhost", port=18080):
        self.host = host
        self.port = port
        self.server = None
        self.clients = []
        self.messages_received = []
        self.messages_to_send = []
        self.running = False
        
    async def handler(self, websocket, path):
        """Handle incoming WebSocket connections"""
        self.clients.append(websocket)
        print(f"Mock broker: Client connected from {websocket.remote_address}")
        
        try:
            # Send any queued messages
            for msg in self.messages_to_send:
                await websocket.send(json.dumps(msg))
                await asyncio.sleep(0.1)  # Small delay to ensure ordering
                
            # Listen for messages
            async for message in websocket:
                data = json.loads(message)
                self.messages_received.append(data)
                print(f"Mock broker received: {data['type']}")
                
                # Auto-respond to certain message types
                await self._auto_respond(websocket, data)
                
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if websocket in self.clients:
                self.clients.remove(websocket)
                
    async def _auto_respond(self, websocket, message):
        """Auto-respond to certain message types"""
        if message.get("type") == "HELLO":
            # Send ACK response
            ack_response = {
                "type": "ACK",
                "heartbeat_interval_sec": 10,
                "worker_id": message.get("worker_id")
            }
            await websocket.send(json.dumps(ack_response))
            
        elif message.get("type") == "HEARTBEAT":
            # Heartbeats don't need responses normally
            pass
            
    async def start(self):
        """Start the mock broker server"""
        self.running = True
        self.server = await websockets.serve(
            self.handler, self.host, self.port
        )
        print(f"Mock broker server started on {self.host}:{self.port}")
        
    async def stop(self):
        """Stop the mock broker server"""
        self.running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            
    def add_message_to_send(self, message):
        """Queue a message to send to next connecting client"""
        self.messages_to_send.append(message)
        
    def get_received_messages(self):
        """Get all messages received from clients"""
        return self.messages_received.copy()
        
    def clear_received_messages(self):
        """Clear the received messages log"""
        self.messages_received.clear()


class TestProtocolCompliance:
    """Test WebSocket protocol compliance"""
    
    @pytest.fixture
    async def mock_broker(self):
        """Start mock broker server for testing"""
        server = MockBrokerServer()
        try:
            await server.start()
            # Give server time to start
            await asyncio.sleep(0.1)
            yield server
        finally:
            await server.stop()
        
    @pytest.fixture
    def client(self):
        """Create worker client configured for testing"""
        with patch.object(constants, 'BROKER_WS_URL', 'ws://localhost:18080/v1/ws'):
            with patch.object(constants, 'X_WORKER_TOKEN', 'test-token'):
                with patch.object(constants, 'HTTP_PORT', 8080):
                    client = BrokerWorkerClient()
                    yield client
                    
    @pytest.mark.asyncio
    async def test_hello_ack_handshake(self, mock_broker, client):
        """Test complete HELLO/ACK handshake flow"""
        
        # Mock the models endpoint
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={
                "data": [{"id": "test-model", "object": "model"}]
            })
            
            # Start client connection task
            client_task = asyncio.create_task(client.start())
            
            # Give time for connection and HELLO message
            await asyncio.sleep(0.5)
            
            # Check messages received by broker
            messages = mock_broker.get_received_messages()
            assert len(messages) >= 1
            
            hello_msg = messages[0]
            assert hello_msg["type"] == "HELLO"
            assert "worker_id" in hello_msg
            assert "models" in hello_msg
            assert "capacity" in hello_msg
            assert "capabilities" in hello_msg
            
            # Verify capabilities structure
            capabilities = hello_msg["capabilities"]
            assert "compute_type" in capabilities
            assert "gpu_model" in capabilities
            assert "features" in capabilities
            assert "streaming" in capabilities["features"]
            assert "pow_injection" in capabilities["features"]
            
            # Stop client
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    async def test_challenge_response_flow(self, mock_broker, client):
        """Test CHALLENGE/CHALLENGE_RESP flow"""
        
        with patch.object(constants, 'CHALLENGE_SECRET', 'test-secret'):
            # Queue challenge message
            challenge_msg = {
                "type": "CHALLENGE",
                "nonce": "test-nonce-12345",
                "timestamp": int(time.time())
            }
            mock_broker.add_message_to_send(challenge_msg)
            
            with aioresponses() as m:
                m.get('http://localhost:8080/v1/models', payload={"data": []})
                
                client_task = asyncio.create_task(client.start())
                await asyncio.sleep(0.8)  # Wait for challenge processing
                
                messages = mock_broker.get_received_messages()
                
                # Should have HELLO and CHALLENGE_RESP
                assert len(messages) >= 2
                
                challenge_resp = None
                for msg in messages:
                    if msg.get("type") == "CHALLENGE_RESP":
                        challenge_resp = msg
                        break
                        
                assert challenge_resp is not None
                assert challenge_resp["nonce"] == "test-nonce-12345"
                assert "proof" in challenge_resp
                
                await client.stop()
                client_task.cancel()
                
    @pytest.mark.asyncio 
    async def test_heartbeat_messages(self, mock_broker, client):
        """Test heartbeat message transmission"""
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.get('http://localhost:8080/status', payload={
                "proxy": {"input_tokens_per_sec": 10.0, "output_tokens_per_sec": 5.0}
            })
            
            client_task = asyncio.create_task(client.start())
            
            # Wait long enough for at least one heartbeat (10s interval from ACK)
            await asyncio.sleep(12.0)
            
            messages = mock_broker.get_received_messages()
            
            # Should have HELLO and at least one HEARTBEAT
            heartbeats = [msg for msg in messages if msg.get("type") == "HEARTBEAT"]
            assert len(heartbeats) >= 1
            
            heartbeat = heartbeats[0]
            assert "busy" in heartbeat
            assert "input_tokens_per_sec" in heartbeat  
            assert "output_tokens_per_sec" in heartbeat
            assert "error_rate" in heartbeat
            assert "queue_depth" in heartbeat
            
            await client.stop()
            client_task.cancel()


class TestJobProcessingProtocol:
    """Test job processing message flow"""
    
    @pytest.fixture
    async def mock_broker(self):
        server = MockBrokerServer()
        await server.start()
        yield server
        await server.stop()
        
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'BROKER_WS_URL', 'ws://localhost:18080/v1/ws'):
            with patch.object(constants, 'X_WORKER_TOKEN', 'test-token'):
                with patch.object(constants, 'HTTP_PORT', 8080):
                    client = BrokerWorkerClient()
                    yield client
                    
    @pytest.mark.asyncio
    async def test_start_job_streaming_response(self, mock_broker, client):
        """Test START message processing with streaming response"""
        
        # Mock streaming response from miner proxy
        streaming_response = [
            'data: {"id": "cmpl-test-123", "choices": [{"delta": {"content": "Hello"}}]}\n\n',
            'data: {"id": "cmpl-test-123", "choices": [{"delta": {"content": " world"}}]}\n\n',
            'data: [DONE]\n\n'
        ]
        
        # Queue START job message
        start_msg = {
            "type": "START",
            "job_id": "job-456",
            "payload": {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "test-model",
                "stream": True
            }
        }
        mock_broker.add_message_to_send(start_msg)
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            
            # Mock streaming POST response
            m.post('http://localhost:8080/v1/chat/completions', 
                   body=''.join(streaming_response), 
                   content_type='text/plain')
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(1.0)  # Wait for job processing
            
            messages = mock_broker.get_received_messages()
            
            # Should have HELLO, and CHUNK/END messages
            chunks = [msg for msg in messages if msg.get("type") == "CHUNK"]
            end_msgs = [msg for msg in messages if msg.get("type") == "END"]
            
            assert len(chunks) >= 2  # "Hello" and " world"
            assert len(end_msgs) == 1
            
            # Verify CHUNK message structure
            chunk = chunks[0]
            assert chunk["job_id"] == "job-456" 
            assert chunk["completion_id"] == "cmpl-test-123"
            assert "delta" in chunk
            
            # Verify END message structure
            end_msg = end_msgs[0]
            assert end_msg["job_id"] == "job-456"
            assert end_msg["completion_id"] == "cmpl-test-123"
            assert "usage" in end_msg
            
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    async def test_start_job_non_streaming_response(self, mock_broker, client):
        """Test START message processing with non-streaming response"""
        
        # Queue START job message
        start_msg = {
            "type": "START", 
            "job_id": "job-789",
            "payload": {
                "messages": [{"role": "user", "content": "Hello"}],
                "model": "test-model",
                "stream": False
            }
        }
        mock_broker.add_message_to_send(start_msg)
        
        # Mock non-streaming response
        mock_response = {
            "id": "cmpl-test-456",
            "choices": [{
                "message": {"content": "Hello! How can I help you today?"}
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8}
        }
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.post('http://localhost:8080/v1/chat/completions', 
                   payload=mock_response)
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(1.0)
            
            messages = mock_broker.get_received_messages()
            
            chunks = [msg for msg in messages if msg.get("type") == "CHUNK"]
            end_msgs = [msg for msg in messages if msg.get("type") == "END"]
            
            assert len(chunks) == 1  # Single chunk for non-streaming
            assert len(end_msgs) == 1
            
            chunk = chunks[0]
            assert chunk["completion_id"] == "cmpl-test-456"
            assert chunk["delta"] == "Hello! How can I help you today?"
            
            end_msg = end_msgs[0]
            assert end_msg["completion_id"] == "cmpl-test-456"
            assert end_msg["usage"]["completion_tokens"] == 8
            
            await client.stop()
            client_task.cancel()


class TestProofRequestProtocol:
    """Test proof request/response protocol"""
    
    @pytest.fixture
    async def mock_broker(self):
        server = MockBrokerServer()
        await server.start()
        yield server
        await server.stop()
        
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'BROKER_WS_URL', 'ws://localhost:18080/v1/ws'):
            with patch.object(constants, 'X_WORKER_TOKEN', 'test-token'):
                with patch.object(constants, 'HTTP_PORT', 8080):
                    client = BrokerWorkerClient()
                    yield client
                    
    @pytest.mark.asyncio
    async def test_proof_request_success(self, mock_broker, client):
        """Test successful proof request handling"""
        
        # Queue PROOF_REQUEST message
        proof_request_msg = {
            "type": "PROOF_REQUEST",
            "completion_id": "cmpl-proof-test-123"
        }
        mock_broker.add_message_to_send(proof_request_msg)
        
        # Mock proof blob
        mock_proof_blob = b"mock_proof_data_12345"
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.get('http://localhost:8080/v1/proof/cmpl-proof-test-123',
                  body=mock_proof_blob,
                  status=200)
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(0.8)
            
            messages = mock_broker.get_received_messages()
            
            proof_results = [msg for msg in messages if msg.get("type") == "PROOF_RESULT"]
            assert len(proof_results) == 1
            
            proof_result = proof_results[0]
            assert proof_result["completion_id"] == "cmpl-proof-test-123"
            assert "proof_b64" in proof_result
            
            # Verify proof data integrity
            import base64
            decoded_proof = base64.b64decode(proof_result["proof_b64"])
            assert decoded_proof == mock_proof_blob
            
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    async def test_proof_request_not_found(self, mock_broker, client):
        """Test proof request when proof not available"""
        
        proof_request_msg = {
            "type": "PROOF_REQUEST",
            "completion_id": "cmpl-missing-proof"
        }
        mock_broker.add_message_to_send(proof_request_msg)
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.get('http://localhost:8080/v1/proof/cmpl-missing-proof', 
                  status=404)
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(0.8)
            
            messages = mock_broker.get_received_messages()
            
            proof_results = [msg for msg in messages if msg.get("type") == "PROOF_RESULT"]
            assert len(proof_results) == 1
            
            proof_result = proof_results[0]
            assert proof_result["completion_id"] == "cmpl-missing-proof"
            assert "error" in proof_result
            assert proof_result["error"] == "proof_not_ready"
            
            await client.stop() 
            client_task.cancel()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])