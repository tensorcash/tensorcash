"""
End-to-end broker integration tests
Tests the complete workflow from broker connection to job completion
Validates all critical features working together
"""
import pytest
import asyncio
import json
import websockets
import threading
import time
import uuid
import base64
from unittest.mock import Mock, AsyncMock, patch
from aioresponses import aioresponses

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from worker_client import BrokerWorkerClient
from components import constants


class FullBrokerSimulator:
    """Complete broker simulator for end-to-end testing"""
    
    def __init__(self, host="localhost", port=18080):
        self.host = host
        self.port = port
        self.server = None
        self.connected_workers = {}
        self.job_queue = []
        self.completed_jobs = []
        self.audit_requests = {}
        self.running = False
        
    async def start(self):
        """Start the full broker simulator"""
        self.running = True
        self.server = await websockets.serve(
            self.handle_worker_connection,
            self.host,
            self.port
        )
        print(f"Full broker simulator started on {self.host}:{self.port}")
        
    async def stop(self):
        """Stop the broker simulator"""
        self.running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            
    async def handle_worker_connection(self, websocket, path):
        """Handle worker WebSocket connections"""
        worker_info = None
        
        try:
            print(f"Broker: Worker connected from {websocket.remote_address}")
            
            async for message in websocket:
                data = json.loads(message)
                msg_type = data.get("type")
                
                if msg_type == "HELLO":
                    worker_info = await self._handle_hello(websocket, data)
                elif msg_type == "ACK":
                    # Job ACK - worker acknowledging it received a START
                    await self._handle_job_ack(websocket, data, worker_info)
                elif msg_type == "HEARTBEAT":
                    await self._handle_heartbeat(websocket, data, worker_info)
                elif msg_type == "CHUNK":
                    await self._handle_chunk(websocket, data, worker_info)
                elif msg_type == "END":
                    await self._handle_end(websocket, data, worker_info)
                elif msg_type == "ERROR":
                    await self._handle_error(websocket, data, worker_info)
                elif msg_type == "PROOF_RESULT":
                    await self._handle_proof_result(websocket, data, worker_info)
                    
        except websockets.exceptions.ConnectionClosed:
            print(f"Broker: Worker {worker_info.get('worker_id') if worker_info else 'unknown'} disconnected")
        except Exception as e:
            print(f"Broker: Error handling worker: {e}")
        finally:
            if worker_info:
                self.connected_workers.pop(worker_info['worker_id'], None)
                
    async def _handle_hello(self, websocket, data):
        """Handle HELLO registration"""
        worker_id = data.get("worker_id")
        models = data.get("models", [])
        capabilities = data.get("capabilities", {})
        
        worker_info = {
            "worker_id": worker_id,
            "websocket": websocket,
            "models": models,
            "capabilities": capabilities,
            "registered_at": time.time(),
            "jobs_processed": 0
        }
        
        self.connected_workers[worker_id] = worker_info
        
        # Send ACK
        ack_response = {
            "type": "ACK",
            "worker_id": worker_id,
            "heartbeat_interval_sec": 10,
            "status": "registered"
        }
        await websocket.send(json.dumps(ack_response))
        print(f"Broker: Registered worker {worker_id} with {len(models)} models")
        
        return worker_info
        
    async def _handle_heartbeat(self, websocket, data, worker_info):
        """Handle heartbeat messages"""
        if worker_info:
            worker_info["last_heartbeat"] = time.time()
            worker_info["metrics"] = {
                "busy": data.get("busy", 0),
                "input_tps": data.get("input_tokens_per_sec", 0.0),
                "output_tps": data.get("output_tokens_per_sec", 0.0),
                "error_rate": data.get("error_rate", 0.0)
            }

    async def _handle_job_ack(self, websocket, data, worker_info):
        """Handle job ACK - worker confirms it received START"""
        job_id = data.get("job_id")
        if job_id:
            # Find job and mark as acknowledged
            for job in self.job_queue:
                if job["job_id"] == job_id:
                    job["acked_at"] = time.time()
                    print(f"Broker: Received ACK for job {job_id}")
                    break

    async def _handle_chunk(self, websocket, data, worker_info):
        """Handle CHUNK messages from worker"""
        job_id = data.get("job_id")
        completion_id = data.get("completion_id")
        delta = data.get("delta")
        
        # Find the job and update it
        for job in self.job_queue:
            if job["job_id"] == job_id:
                if not job.get("completion_id"):
                    # First chunk - bind completion_id
                    job["completion_id"] = completion_id
                    print(f"Broker: Bound completion_id {completion_id} to job {job_id}")
                    
                job.setdefault("chunks", []).append(delta)
                break
                
    async def _handle_end(self, websocket, data, worker_info):
        """Handle END messages from worker"""
        job_id = data.get("job_id")
        completion_id = data.get("completion_id")
        usage = data.get("usage", {})
        
        # Mark job as completed
        for i, job in enumerate(self.job_queue):
            if job["job_id"] == job_id:
                completed_job = self.job_queue.pop(i)
                completed_job.update({
                    "completion_id": completion_id,
                    "usage": usage,
                    "completed_at": time.time(),
                    "worker_id": worker_info.get("worker_id") if worker_info else None
                })
                self.completed_jobs.append(completed_job)
                
                if worker_info:
                    worker_info["jobs_processed"] += 1
                    
                print(f"Broker: Job {job_id} completed with completion_id {completion_id}")
                
                # Trigger audit with some probability
                if len(self.completed_jobs) % 3 == 0:  # Audit every 3rd job
                    await self._request_proof_audit(websocket, completion_id)
                    
                break
                
    async def _handle_error(self, websocket, data, worker_info):
        """Handle ERROR messages from worker"""
        job_id = data.get("job_id")
        error = data.get("error")
        
        # Mark job as failed
        for i, job in enumerate(self.job_queue):
            if job["job_id"] == job_id:
                failed_job = self.job_queue.pop(i)
                failed_job.update({
                    "error": error,
                    "failed_at": time.time(),
                    "worker_id": worker_info.get("worker_id") if worker_info else None
                })
                self.completed_jobs.append(failed_job)
                print(f"Broker: Job {job_id} failed: {error}")
                break
                
    async def _handle_proof_result(self, websocket, data, worker_info):
        """Handle PROOF_RESULT messages from worker"""
        completion_id = data.get("completion_id")
        proof_b64 = data.get("proof_b64")
        error = data.get("error")
        
        if completion_id in self.audit_requests:
            audit = self.audit_requests[completion_id]
            audit.update({
                "result_received_at": time.time(),
                "proof_b64": proof_b64,
                "error": error,
                "worker_id": worker_info.get("worker_id") if worker_info else None
            })
            
            if proof_b64:
                # Simulate verification
                audit["verification_status"] = "verified"
                print(f"Broker: Received and verified proof for {completion_id}")
            else:
                audit["verification_status"] = "failed"
                print(f"Broker: Proof audit failed for {completion_id}: {error}")
                
    async def _request_proof_audit(self, websocket, completion_id):
        """Request proof audit from worker"""
        audit_request = {
            "type": "PROOF_REQUEST",
            "completion_id": completion_id
        }
        
        self.audit_requests[completion_id] = {
            "requested_at": time.time(),
            "completion_id": completion_id
        }
        
        await websocket.send(json.dumps(audit_request))
        print(f"Broker: Requested proof audit for {completion_id}")
        
    async def submit_job(self, job_payload, stream=False):
        """Submit job to be processed (simulates external API call)"""
        job_id = f"job-e2e-{uuid.uuid4()}"
        
        job = {
            "job_id": job_id,
            "payload": job_payload,
            "stream": stream,
            "submitted_at": time.time()
        }
        
        self.job_queue.append(job)
        
        # Send to first available worker
        for worker_info in self.connected_workers.values():
            if worker_info:
                start_msg = {
                    "type": "START",
                    "job_id": job_id,
                    "payload": job_payload
                }
                await worker_info["websocket"].send(json.dumps(start_msg))
                print(f"Broker: Sent job {job_id} to worker {worker_info['worker_id']}")
                break
                
        return job_id
        
    def get_job_status(self, job_id):
        """Get status of a job"""
        # Check queue
        for job in self.job_queue:
            if job["job_id"] == job_id:
                return {"status": "processing", "job": job}
                
        # Check completed
        for job in self.completed_jobs:
            if job["job_id"] == job_id:
                return {"status": "completed", "job": job}
                
        return {"status": "not_found"}
        
    def get_audit_status(self, completion_id):
        """Get audit status for completion_id"""
        return self.audit_requests.get(completion_id, {"status": "no_audit"})


class TestFullBrokerIntegration:
    """End-to-end integration tests with full broker simulation"""
    
    @pytest.fixture
    async def broker_sim(self):
        """Start full broker simulator"""
        simulator = FullBrokerSimulator()
        await simulator.start()
        yield simulator
        await simulator.stop()
        
    @pytest.fixture
    def client(self):
        """Create worker client for testing"""
        with patch.object(constants, 'BROKER_WS_URL', 'ws://localhost:18080/v1/ws'):
            with patch.object(constants, 'X_WORKER_TOKEN', 'test-e2e-token'):
                with patch.object(constants, 'HTTP_PORT', 8080):
                    client = BrokerWorkerClient()
                    yield client
                    
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_complete_job_workflow_streaming(self, broker_sim, client):
        """Test complete streaming job workflow from registration to completion"""
        
        test_completion_id = f"cmpl-e2e-streaming-{uuid.uuid4()}"
        
        # Mock vLLM streaming response
        streaming_chunks = [
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": "The"}}}}]}}',
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": " quick"}}}}]}}',
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": " brown fox"}}}}]}}'
        ]
        
        with aioresponses() as m:
            # Mock models endpoint
            m.get('http://localhost:8080/v1/models', payload={
                "data": [{"id": "test-model", "object": "model"}]
            })
            
            # Mock streaming completion
            m.post('http://localhost:8080/v1/chat/completions', 
                   body='\n'.join([f"data: {chunk}" for chunk in streaming_chunks]) + '\ndata: [DONE]\n')
            
            # Mock status endpoint for metrics
            m.get('http://localhost:8080/status', payload={
                "proxy": {"input_tokens_per_sec": 100.0, "output_tokens_per_sec": 25.0}
            })
            
            # Start worker
            client_task = asyncio.create_task(client.start())
            
            # Wait for registration
            await asyncio.sleep(1.0)
            
            # Verify worker registered
            assert len(broker_sim.connected_workers) == 1
            worker = list(broker_sim.connected_workers.values())[0]
            assert worker["models"] == ["test-model"]
            
            # Submit job through broker
            job_payload = {
                "messages": [{"role": "user", "content": "Tell me about foxes"}],
                "model": "test-model",
                "stream": True
            }
            
            job_id = await broker_sim.submit_job(job_payload, stream=True)
            
            # Wait for job processing
            await asyncio.sleep(2.0)
            
            # Verify job completion
            job_status = broker_sim.get_job_status(job_id)
            assert job_status["status"] == "completed"
            
            completed_job = job_status["job"]
            assert completed_job["completion_id"] == test_completion_id
            assert completed_job["chunks"] == ["The", " quick", " brown fox"]
            assert completed_job["worker_id"] == worker["worker_id"]
            
            # Clean up
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_complete_job_workflow_non_streaming(self, broker_sim, client):
        """Test complete non-streaming job workflow"""
        
        test_completion_id = f"cmpl-e2e-non-streaming-{uuid.uuid4()}"
        
        mock_response = {
            "id": test_completion_id,
            "choices": [{
                "message": {"content": "Foxes are fascinating creatures!"}
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 5}
        }
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.post('http://localhost:8080/v1/chat/completions', payload=mock_response)
            m.get('http://localhost:8080/status', payload={"proxy": {}})
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(1.0)
            
            # Submit non-streaming job
            job_payload = {
                "messages": [{"role": "user", "content": "What are foxes?"}],
                "model": "test-model",
                "stream": False
            }
            
            job_id = await broker_sim.submit_job(job_payload, stream=False)
            await asyncio.sleep(1.5)
            
            # Verify completion
            job_status = broker_sim.get_job_status(job_id)
            assert job_status["status"] == "completed"
            
            completed_job = job_status["job"]
            assert completed_job["completion_id"] == test_completion_id
            assert completed_job["chunks"] == ["Foxes are fascinating creatures!"]
            assert completed_job["usage"]["completion_tokens"] == 5
            
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_proof_audit_workflow(self, broker_sim, client):
        """Test complete proof audit workflow"""
        
        test_completion_id = f"cmpl-e2e-audit-{uuid.uuid4()}"
        mock_proof_data = b"mock_proof_flatbuffer_" + os.urandom(32)
        
        mock_response = {
            "id": test_completion_id,
            "choices": [{"message": {"content": "Test response for audit"}}],
            "usage": {"completion_tokens": 4}
        }
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.post('http://localhost:8080/v1/chat/completions', payload=mock_response)
            m.get('http://localhost:8080/status', payload={"proxy": {}})
            
            # Mock proof endpoint
            m.get(f'http://localhost:8080/v1/proof/{test_completion_id}',
                  body=mock_proof_data, status=200)
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(1.0)
            
            # Submit multiple jobs to trigger audit (every 3rd job gets audited)
            for i in range(3):
                job_payload = {
                    "messages": [{"role": "user", "content": f"Test message {i}"}],
                    "model": "test-model"
                }
                await broker_sim.submit_job(job_payload)
                await asyncio.sleep(0.5)
                
            # Wait for audit processing
            await asyncio.sleep(2.0)
            
            # Should have triggered an audit for the 3rd job
            audits = [audit for audit in broker_sim.audit_requests.values() 
                     if audit.get("verification_status") == "verified"]
            
            assert len(audits) >= 1, "Should have at least one successful audit"
            
            audit = audits[0]
            assert "proof_b64" in audit
            
            # Verify proof data integrity
            decoded_proof = base64.b64decode(audit["proof_b64"])
            assert decoded_proof == mock_proof_data
            
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    async def test_error_handling_missing_completion_id(self, broker_sim, client):
        """Test error handling when vLLM response lacks completion_id"""
        
        # Malformed response without completion_id
        mock_response = {
            "choices": [{"message": {"content": "Response without ID"}}],
            "usage": {"completion_tokens": 3}
            # Missing "id" field!
        }
        
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.post('http://localhost:8080/v1/chat/completions', payload=mock_response)
            m.get('http://localhost:8080/status', payload={"proxy": {}})
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(1.0)
            
            # Submit job that will fail
            job_payload = {
                "messages": [{"role": "user", "content": "This will fail"}],
                "model": "test-model"
            }
            
            job_id = await broker_sim.submit_job(job_payload)
            await asyncio.sleep(1.5)
            
            # Verify job failed appropriately
            job_status = broker_sim.get_job_status(job_id)
            assert job_status["status"] == "completed"  # Completed with error
            
            failed_job = job_status["job"]
            assert "error" in failed_job
            assert "No completion_id in vLLM response" in failed_job["error"]
            
            await client.stop()
            client_task.cancel()
            
    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_worker_reconnection_resilience(self, broker_sim, client):
        """Test worker reconnection after network issues"""
        
        # Start worker
        with aioresponses() as m:
            m.get('http://localhost:8080/v1/models', payload={"data": []})
            m.get('http://localhost:8080/status', payload={"proxy": {}})
            
            client_task = asyncio.create_task(client.start())
            await asyncio.sleep(1.0)
            
            # Verify initial connection
            assert len(broker_sim.connected_workers) == 1
            initial_worker_id = list(broker_sim.connected_workers.keys())[0]
            
            # Simulate network disconnection by stopping broker temporarily
            await broker_sim.stop()
            await asyncio.sleep(1.0)  # Let disconnect be detected
            
            # Restart broker
            await broker_sim.start()
            await asyncio.sleep(3.0)  # Wait for reconnection with backoff
            
            # Verify reconnection
            assert len(broker_sim.connected_workers) == 1
            
            # Should be same worker (same worker_id)
            reconnected_worker_id = list(broker_sim.connected_workers.keys())[0]
            assert reconnected_worker_id == initial_worker_id
            
            await client.stop()
            client_task.cancel()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--timeout=60"])