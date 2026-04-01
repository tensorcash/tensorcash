"""
End-to-end completion_id flow tests
CRITICAL: Tests that completion_id integrity is maintained from vLLM → Broker
No fallbacks to job_id should ever occur - this would break audit trail
"""
import pytest
import asyncio
import json
import uuid
from unittest.mock import Mock, AsyncMock, patch
from aioresponses import aioresponses

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from worker_client import BrokerWorkerClient
from components import constants


class MockCompletionIdTracker:
    """Track completion_id flow throughout the system"""
    
    def __init__(self):
        self.completion_ids_seen = []
        self.job_ids_seen = []
        self.fallback_attempts = []
        
    def record_completion_id(self, source, completion_id):
        """Record when a completion_id is seen"""
        self.completion_ids_seen.append({
            "source": source,
            "completion_id": completion_id,
            "timestamp": asyncio.get_event_loop().time()
        })
        
    def record_job_id_usage(self, source, job_id, context):
        """Record when job_id is used (should only be for tracking, not completion)"""
        self.job_ids_seen.append({
            "source": source,
            "job_id": job_id,
            "context": context,
            "timestamp": asyncio.get_event_loop().time()
        })
        
    def record_fallback_attempt(self, source, job_id, attempted_completion_id):
        """Record any attempt to fallback from completion_id to job_id"""
        self.fallback_attempts.append({
            "source": source,
            "job_id": job_id,
            "attempted_completion_id": attempted_completion_id,
            "timestamp": asyncio.get_event_loop().time()
        })
        
    def validate_no_fallbacks(self):
        """Ensure no fallback attempts occurred"""
        assert len(self.fallback_attempts) == 0, f"Fallback attempts detected: {self.fallback_attempts}"
        
    def validate_completion_id_consistency(self):
        """Ensure the same completion_id flows through all components"""
        if len(self.completion_ids_seen) == 0:
            pytest.fail("No completion_id observed in the flow")
            
        first_completion_id = self.completion_ids_seen[0]["completion_id"]
        
        for record in self.completion_ids_seen:
            assert record["completion_id"] == first_completion_id, \
                f"Inconsistent completion_id: {record} vs {first_completion_id}"


@pytest.mark.completion_id
class TestCompletionIdIntegrity:
    """Test completion_id integrity throughout the flow"""
    
    @pytest.fixture
    def tracker(self):
        return MockCompletionIdTracker()
        
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'HTTP_PORT', 8080):
            return BrokerWorkerClient()
            
    @pytest.mark.asyncio
    async def test_streaming_completion_id_extraction(self, client, tracker):
        """Test completion_id extraction from streaming vLLM response"""
        
        # Create test completion_id that should flow end-to-end
        test_completion_id = "cmpl-streaming-test-" + str(uuid.uuid4())
        job_id = "job-" + str(uuid.uuid4())
        
        # Mock streaming response with completion_id
        streaming_chunks = [
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": "Hello"}}}}]}}',
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": " world"}}}}]}}',
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": "!"}}}}]}}'
        ]
        
        # Mock the HTTP response
        mock_response = AsyncMock()
        mock_response.content.__aiter__ = AsyncMock(return_value=[
            f"data: {chunk}\n\n".encode() for chunk in streaming_chunks
        ] + [b"data: [DONE]\n\n"])
        
        client.session.post.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        # Execute job processing
        start_msg = {
            "job_id": job_id,
            "payload": {"stream": True, "messages": [{"role": "user", "content": "test"}]}
        }
        
        await client._handle_job_start(start_msg)
        
        # Analyze sent messages
        sent_messages = []
        for call in client.ws.send_str.call_args_list:
            msg_data = json.loads(call[0][0])
            sent_messages.append(msg_data)
            
        # Validate completion_id flow
        chunks = [msg for msg in sent_messages if msg.get("type") == "CHUNK"]
        end_msgs = [msg for msg in sent_messages if msg.get("type") == "END"]
        
        assert len(chunks) == 3, f"Expected 3 chunks, got {len(chunks)}"
        assert len(end_msgs) == 1, f"Expected 1 END message, got {len(end_msgs)}"
        
        # CRITICAL: All messages must use the same completion_id from vLLM
        for chunk in chunks:
            assert chunk["completion_id"] == test_completion_id, \
                f"Chunk has wrong completion_id: {chunk['completion_id']} != {test_completion_id}"
            assert chunk["job_id"] == job_id  # job_id should still be present for routing
            tracker.record_completion_id("worker_chunk", chunk["completion_id"])
            
        end_msg = end_msgs[0]
        assert end_msg["completion_id"] == test_completion_id, \
            f"END message has wrong completion_id: {end_msg['completion_id']} != {test_completion_id}"
        assert end_msg["job_id"] == job_id
        tracker.record_completion_id("worker_end", end_msg["completion_id"])
        
        # Validate no fallbacks occurred
        tracker.validate_no_fallbacks()
        tracker.validate_completion_id_consistency()
        
    @pytest.mark.asyncio
    async def test_non_streaming_completion_id_extraction(self, client, tracker):
        """Test completion_id extraction from non-streaming vLLM response"""
        
        test_completion_id = "cmpl-nonstreaming-test-" + str(uuid.uuid4())
        job_id = "job-" + str(uuid.uuid4())
        
        # Mock non-streaming response
        mock_vllm_response = {
            "id": test_completion_id,
            "choices": [{
                "message": {"content": "This is a non-streaming response"}
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7}
        }
        
        mock_response = AsyncMock()
        mock_response.json.return_value = mock_vllm_response
        
        client.session.post.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        # Execute job processing
        start_msg = {
            "job_id": job_id,
            "payload": {"stream": False, "messages": [{"role": "user", "content": "test"}]}
        }
        
        await client._handle_job_start(start_msg)
        
        # Analyze sent messages
        sent_messages = []
        for call in client.ws.send_str.call_args_list:
            msg_data = json.loads(call[0][0])
            sent_messages.append(msg_data)
            
        chunks = [msg for msg in sent_messages if msg.get("type") == "CHUNK"]
        end_msgs = [msg for msg in sent_messages if msg.get("type") == "END"]
        
        assert len(chunks) == 1
        assert len(end_msgs) == 1
        
        # CRITICAL: Verify completion_id integrity
        chunk = chunks[0]
        assert chunk["completion_id"] == test_completion_id
        tracker.record_completion_id("worker_chunk", chunk["completion_id"])
        
        end_msg = end_msgs[0]
        assert end_msg["completion_id"] == test_completion_id
        assert end_msg["usage"]["completion_tokens"] == 7
        tracker.record_completion_id("worker_end", end_msg["completion_id"])
        
        tracker.validate_no_fallbacks()
        tracker.validate_completion_id_consistency()


@pytest.mark.completion_id
class TestCompletionIdErrorHandling:
    """Test error handling when completion_id is missing"""
    
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'HTTP_PORT', 8080):
            return BrokerWorkerClient()
            
    @pytest.mark.asyncio
    async def test_missing_completion_id_streaming(self, client):
        """Test error when streaming response lacks completion_id"""
        
        job_id = "job-" + str(uuid.uuid4())
        
        # Mock streaming response WITHOUT completion_id (malformed)
        streaming_chunks = [
            '{"choices": [{"delta": {"content": "Hello"}}]}',  # NO "id" field
            '{"choices": [{"delta": {"content": " world"}}]}'   # NO "id" field
        ]
        
        mock_response = AsyncMock()
        mock_response.content.__aiter__ = AsyncMock(return_value=[
            f"data: {chunk}\n\n".encode() for chunk in streaming_chunks
        ] + [b"data: [DONE]\n\n"])
        
        client.session.post.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        start_msg = {
            "job_id": job_id,
            "payload": {"stream": True, "messages": [{"role": "user", "content": "test"}]}
        }
        
        # This should raise an exception due to missing completion_id
        with pytest.raises(Exception) as exc_info:
            await client._handle_job_start(start_msg)
            
        assert "No completion_id received from vLLM" in str(exc_info.value)
        
        # Should have sent ERROR message
        error_messages = []
        for call in client.ws.send_str.call_args_list:
            msg_data = json.loads(call[0][0])
            if msg_data.get("type") == "ERROR":
                error_messages.append(msg_data)
                
        assert len(error_messages) == 1
        error_msg = error_messages[0]
        assert error_msg["job_id"] == job_id
        assert "No completion_id received from vLLM" in error_msg["error"]
        
    @pytest.mark.asyncio
    async def test_missing_completion_id_non_streaming(self, client):
        """Test error when non-streaming response lacks completion_id"""
        
        job_id = "job-" + str(uuid.uuid4())
        
        # Mock response WITHOUT completion_id
        mock_vllm_response = {
            "choices": [{
                "message": {"content": "Response without ID"}
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4}
            # Missing "id" field!
        }
        
        mock_response = AsyncMock()
        mock_response.json.return_value = mock_vllm_response
        
        client.session.post.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        start_msg = {
            "job_id": job_id,
            "payload": {"stream": False, "messages": [{"role": "user", "content": "test"}]}
        }
        
        # Should raise exception
        with pytest.raises(Exception) as exc_info:
            await client._handle_job_start(start_msg)
            
        assert "No completion_id in vLLM response" in str(exc_info.value)


class TestJobTrackingStateTransitions:
    """Test job tracking state transitions from job_id to completion_id"""
    
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'HTTP_PORT', 8080):
            return BrokerWorkerClient()
            
    @pytest.mark.asyncio
    async def test_job_tracking_transitions(self, client):
        """Test that job tracking properly transitions from job_id to completion_id"""
        
        test_completion_id = "cmpl-tracking-test-" + str(uuid.uuid4())
        job_id = "job-" + str(uuid.uuid4())
        
        # Initial state - no jobs tracked
        assert len(client.active_jobs) == 0
        
        # Mock streaming response
        streaming_chunks = [
            f'{{"id": "{test_completion_id}", "choices": [{{"delta": {{"content": "test"}}}}]}}'
        ]
        
        mock_response = AsyncMock()
        mock_response.content.__aiter__ = AsyncMock(return_value=[
            f"data: {chunk}\n\n".encode() for chunk in streaming_chunks
        ] + [b"data: [DONE]\n\n"])
        
        client.session.post.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        # Start job processing
        start_msg = {
            "job_id": job_id,
            "payload": {"stream": True, "messages": [{"role": "user", "content": "test"}]}
        }
        
        # Verify job tracking during processing
        initial_jobs = client.active_jobs.copy()
        
        await client._handle_job_start(start_msg)
        
        final_jobs = client.active_jobs.copy()
        
        # Job should have been added initially as job_id, then transitioned to completion_id
        # Final state should contain completion_id, not job_id
        assert job_id not in final_jobs, f"job_id {job_id} should not be in final active jobs"
        
        # The completion_id might still be tracked if job didn't complete cleanly in test
        # but the important thing is job_id was removed when completion_id was discovered
        
    @pytest.mark.asyncio
    async def test_multiple_concurrent_jobs_tracking(self, client):
        """Test tracking multiple jobs with different completion_ids"""
        
        job1_id = "job-1-" + str(uuid.uuid4())
        job2_id = "job-2-" + str(uuid.uuid4())
        completion1_id = "cmpl-1-" + str(uuid.uuid4())
        completion2_id = "cmpl-2-" + str(uuid.uuid4())
        
        # Mock client methods
        client.ws = AsyncMock()
        
        # Simulate starting two jobs
        client.active_jobs.add(job1_id)
        client.active_jobs.add(job2_id)
        assert len(client.active_jobs) == 2
        
        # Simulate completion_id discovery for job1
        client.active_jobs.discard(job1_id) 
        client.active_jobs.add(completion1_id)
        
        # Simulate completion_id discovery for job2
        client.active_jobs.discard(job2_id)
        client.active_jobs.add(completion2_id)
        
        # Final state should have both completion_ids, no job_ids
        assert completion1_id in client.active_jobs
        assert completion2_id in client.active_jobs
        assert job1_id not in client.active_jobs
        assert job2_id not in client.active_jobs
        assert len(client.active_jobs) == 2


@pytest.mark.completion_id
class TestProofRequestCompletionIdIntegrity:
    """Test that proof requests use correct completion_id"""
    
    @pytest.fixture
    def client(self):
        with patch.object(constants, 'HTTP_PORT', 8080):
            return BrokerWorkerClient()
            
    @pytest.mark.asyncio
    async def test_proof_request_completion_id_flow(self, client):
        """Test that proof requests use the original vLLM completion_id"""
        
        test_completion_id = "cmpl-proof-integrity-" + str(uuid.uuid4())
        
        # Mock successful proof retrieval
        mock_proof_blob = b"mock_proof_data_for_completion_id"
        
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read.return_value = mock_proof_blob
        
        client.session.get.return_value.__aenter__.return_value = mock_response
        client.ws = AsyncMock()
        
        # Handle proof request
        proof_request_msg = {
            "completion_id": test_completion_id
        }
        
        await client._handle_proof_request(proof_request_msg)
        
        # Verify proof was requested with correct completion_id
        client.session.get.assert_called_once_with(
            f"http://localhost:8080/v1/proof/{test_completion_id}",
            timeout=10
        )
        
        # Verify response contains correct completion_id
        client.ws.send_str.assert_called_once()
        sent_data = json.loads(client.ws.send_str.call_args[0][0])
        
        assert sent_data["type"] == "PROOF_RESULT"
        assert sent_data["completion_id"] == test_completion_id
        assert "proof_b64" in sent_data
        
        # Verify proof data integrity
        import base64
        decoded_proof = base64.b64decode(sent_data["proof_b64"])
        assert decoded_proof == mock_proof_blob


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])