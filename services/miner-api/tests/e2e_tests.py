"""End-to-end tests for mining proxy"""
import os
import time
import json
import requests
import pytest
import asyncio
import aiohttp
from typing import Dict, Any

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:8081")
MOCK_VLLM_URL = os.getenv("MOCK_VLLM_URL", "http://localhost:8000")


class TestProxyE2E:
    """E2E tests for mining proxy functionality"""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup before each test"""
        # Wait for services to be ready
        self.wait_for_service(PROXY_URL + "/health", timeout=30)
        self.wait_for_service(MOCK_VLLM_URL + "/status", timeout=30)
    
    def wait_for_service(self, url, timeout=30):
        """Wait for a service to become available"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get(url, timeout=1)
                if response.status_code == 200:
                    return
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        raise TimeoutError("Service at {} did not become available within {} seconds".format(url, timeout))
    
    def test_health_endpoint(self):
        """Test proxy health endpoint"""
        response = requests.get("{}/health".format(PROXY_URL))
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
    
    def test_status_endpoint(self):
        """Test proxy status endpoint"""
        response = requests.get("{}/status".format(PROXY_URL))
        assert response.status_code == 200
        data = response.json()
        
        # Verify status structure
        assert "context" in data
        assert "vdf" in data
        assert "zmq" in data
        assert "proxy" in data
        
        # Check context details
        context = data["context"]
        assert "block_hash" in context
        assert "request_id" in context
        assert "vdf_tick" in context
        assert "has_vdf_proof" in context
        
        # Check proxy details
        proxy = data["proxy"]
        assert "active_requests" in proxy
        assert "min_active" in proxy
        assert proxy["min_active"] == 0  # As configured in docker-compose
    
    def test_completion_request_with_pow_injection(self):
        """Test that completion requests have PoW data injected"""
        request_data = {
            "model": "Qwen/Qwen3-8B",
            "prompt": "Test prompt for E2E",
            "max_tokens": 50,
            "temperature": 0.7
        }
        
        # Send request through proxy
        response = requests.post(
            "{}/v1/completions".format(PROXY_URL),
            json=request_data,
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200
        result = response.json()
        
        # Verify response structure
        assert "id" in result
        assert "choices" in result
        assert len(result["choices"]) > 0
        
        # Check that mock server received PoW data by querying debug endpoint
        time.sleep(0.5)  # Give time for request to be processed
        debug_response = requests.get("{}/debug/requests".format(MOCK_VLLM_URL))
        debug_data = debug_response.json()
        
        # Find our request
        received_requests = debug_data["requests"]
        our_request = None
        for req in received_requests:
            if req.get("prompt") == "Test prompt for E2E":
                our_request = req
                break
        
        assert our_request is not None, "Request not found in mock server"
        
        # Verify PoW data was injected
        assert "extra_sampling_params" in our_request
        assert "pow" in our_request["extra_sampling_params"]
        
        pow_data = our_request["extra_sampling_params"]["pow"]
        assert "block_hash" in pow_data
        assert "vdf" in pow_data
        assert "tick" in pow_data
        assert "target" in pow_data
        assert "header_prefix" in pow_data
        assert "ipfs_cid" in pow_data
        assert "request_id" in pow_data
        assert "difficulty" in pow_data
        
        # Verify sampling params were added
        assert "top_k" in our_request
        assert "top_p" in our_request
        assert "temperature" in our_request
    
    def test_chat_completion_request(self):
        """Test chat completion endpoint"""
        request_data = {
            "model": "Qwen/Qwen3-8B",
            "messages": [
                {"role": "user", "content": "Hello, this is an E2E test"}
            ],
            "max_tokens": 50
        }
        
        response = requests.post(
            "{}/v1/chat/completions".format(PROXY_URL),
            json=request_data,
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code == 200
        result = response.json()
        
        assert "id" in result
        assert "choices" in result
        assert result["choices"][0]["message"]["role"] == "assistant"
    
    @pytest.mark.asyncio
    async def test_streaming_response(self):
        """Test streaming response handling"""
        request_data = {
            "model": "Qwen/Qwen3-8B",
            "prompt": "Stream test",
            "max_tokens": 50,
            "stream": True
        }
        
        chunks = []
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "{}/v1/completions".format(PROXY_URL),
                json=request_data
            ) as response:
                assert response.status == 200
                assert response.headers.get("Content-Type") == "text/event-stream"
                
                async for line in response.content:
                    line = line.decode('utf-8').strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data != "[DONE]":
                            chunks.append(json.loads(data))
        
        # Verify we received multiple chunks
        assert len(chunks) > 0
        assert all("choices" in chunk for chunk in chunks)
    
    def test_models_endpoint(self):
        """Test models endpoint proxying"""
        response = requests.get("{}/v1/models".format(PROXY_URL))
        assert response.status_code == 200
        
        data = response.json()
        assert "data" in data
        assert len(data["data"]) > 0
        
        # Verify model structure
        model = data["data"][0]
        assert "id" in model
        assert model["id"] == "Qwen/Qwen3-8B"
    
    def test_concurrent_requests(self):
        """Test handling of concurrent requests"""
        import concurrent.futures
        
        def make_request(i):
            request_data = {
                "model": "Qwen/Qwen3-8B",
                "prompt": "Concurrent test {}".format(i),
                "max_tokens": 10
            }
            response = requests.post(
                "{}/v1/completions".format(PROXY_URL),
                json=request_data,
                timeout=10
            )
            return response.status_code
        
        # Send 10 concurrent requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request, i) for i in range(10)]
            results = [f.result() for f in futures]
        
        # All should succeed
        assert all(status == 200 for status in results)
    
    def test_invalid_request_handling(self):
        """Test error handling for invalid requests"""
        # Test missing content-type
        response = requests.post(
            "{}/v1/completions".format(PROXY_URL),
            data="not json"
        )
        assert response.status_code == 400
        
        # Test invalid JSON
        response = requests.post(
            "{}/v1/completions".format(PROXY_URL),
            data="invalid json",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        
        # Test unsupported endpoint
        response = requests.get("{}/v1/unsupported".format(PROXY_URL))
        assert response.status_code == 404
    
    def test_proof_cache_endpoints(self):
        """Test proof cache functionality"""
        # First make a completion request
        request_data = {
            "model": "Qwen/Qwen3-8B",
            "prompt": "Test for proof cache",
            "max_tokens": 10
        }
        
        response = requests.post(
            "{}/v1/completions".format(PROXY_URL),
            json=request_data
        )
        assert response.status_code == 200
        result = response.json()
        completion_id = result.get("id")
        
        if completion_id:
            # Check proof status (may or may not be available depending on collector)
            status_response = requests.get(
                "{}/v1/proof/status/{}".format(PROXY_URL, completion_id)
            )
            # Should return 200 with available=false if no proof yet
            assert status_response.status_code == 200
            status_data = status_response.json()
            assert "completion_id" in status_data
            assert "available" in status_data
            # It's OK if available is False - proof might not be collected yet
            assert isinstance(status_data["available"], bool)
        
        # Check proof stats endpoint
        stats_response = requests.get("{}/v1/proof/stats".format(PROXY_URL))
        if stats_response.status_code == 200:
            stats_data = stats_response.json()
            # Stats should have items or bytes fields
            assert "items" in stats_data or "error" in stats_data
    
    def test_minimum_active_requests(self):
        """Test that proxy maintains minimum active requests"""
        # Get initial status
        response = requests.get("{}/status".format(PROXY_URL))
        data = response.json()
        
        # Wait a bit for dummy requests to be generated
        time.sleep(5)
        
        # Check status again
        response = requests.get("{}/status".format(PROXY_URL))
        data = response.json()
        
        # Should have active requests (real or dummy)
        proxy_status = data["proxy"]
        assert proxy_status["active_requests"] >= 0
        
        # Check if dummy requests are being generated
        if "requests_by_type" in proxy_status:
            assert "real" in proxy_status["requests_by_type"]
            assert "dummy" in proxy_status["requests_by_type"]


class TestProxyResilience:
    """Test proxy resilience and error recovery"""
    
    def test_upstream_timeout_handling(self):
        """Test handling of upstream timeouts"""
        # Send a request that might timeout
        request_data = {
            "model": "Qwen/Qwen3-8B",
            "prompt": "Timeout test",
            "max_tokens": 1000,
            "timeout": 0.001  # Very short timeout
        }
        
        # This should handle the timeout gracefully
        response = requests.post(
            "{}/v1/completions".format(PROXY_URL),
            json=request_data,
            timeout=30
        )
        
        # Should get either success or a proper error response
        assert response.status_code in [200, 500, 504]
    
    def test_recovery_after_errors(self):
        """Test that proxy recovers after errors"""
        # Send some invalid requests
        for i in range(3):
            requests.post(
                "{}/v1/completions".format(PROXY_URL),
                data="invalid",
                headers={"Content-Type": "application/json"}
            )
        
        # Now send a valid request
        request_data = {
            "model": "Qwen/Qwen3-8B",
            "prompt": "Recovery test",
            "max_tokens": 10
        }
        
        response = requests.post(
            "{}/v1/completions".format(PROXY_URL),
            json=request_data
        )
        
        # Should work fine
        assert response.status_code == 200


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])