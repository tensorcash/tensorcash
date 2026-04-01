"""
Pytest configuration and shared fixtures for broker integration tests
"""
import pytest
import asyncio
import json
import os
import tempfile
import uuid
from unittest.mock import patch

# Add src and local_mocks to Python path for all tests
import sys
test_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.join(test_dir, '../src')
mocks_dir = os.path.join(test_dir, 'local_mocks')
sys.path.insert(0, src_dir)
sys.path.insert(0, mocks_dir)


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_constants():
    """Mock constants for consistent test environment"""
    with patch('components.constants.BROKER_WS_URL', 'ws://localhost:18080/v1/ws'):
        with patch('components.constants.HTTP_PORT', 8080):
            with patch('components.constants.X_WORKER_TOKEN', 'test-worker-token'):
                with patch('components.constants.PROVIDER_JWT_TOKEN', ''):
                    with patch('components.constants.CHALLENGE_SECRET', ''):
                        with patch('components.constants.WORKER_CAPACITY', 2):
                            with patch('components.constants.COMPUTE_TYPE', 'test-gpu'):
                                with patch('components.constants.GPU_MODEL', 'TestGPU-16GB'):
                                    with patch('components.constants.GPU_MEMORY_GB', 16):
                                        with patch('components.constants.WORKER_REGION', 'test-region'):
                                            with patch('components.constants.MAX_CONTEXT_WINDOW', 4096):
                                                yield


@pytest.fixture
def test_completion_id():
    """Generate unique completion_id for testing"""
    return f"cmpl-test-{uuid.uuid4()}"


@pytest.fixture  
def test_job_id():
    """Generate unique job_id for testing"""
    return f"job-test-{uuid.uuid4()}"


@pytest.fixture
def sample_jwt_token():
    """Sample JWT token for testing (not a real token)"""
    return "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXdvcmtlciIsImlhdCI6MTY5OTEyMzQ1Nn0.test_signature"


@pytest.fixture
def sample_vllm_streaming_response(test_completion_id):
    """Sample vLLM streaming response chunks"""
    return [
        f'{{"id": "{test_completion_id}", "object": "chat.completion.chunk", "choices": [{{"delta": {{"content": "Hello"}}}}]}}',
        f'{{"id": "{test_completion_id}", "object": "chat.completion.chunk", "choices": [{{"delta": {{"content": " there"}}}}]}}', 
        f'{{"id": "{test_completion_id}", "object": "chat.completion.chunk", "choices": [{{"delta": {{"content": "!"}}}}]}}'
    ]


@pytest.fixture
def sample_vllm_non_streaming_response(test_completion_id):
    """Sample vLLM non-streaming response"""
    return {
        "id": test_completion_id,
        "object": "chat.completion",
        "created": 1699123456,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello! How can I help you today?"
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20
        }
    }


@pytest.fixture
def sample_broker_start_message(test_job_id):
    """Sample broker START message"""
    return {
        "type": "START",
        "job_id": test_job_id,
        "payload": {
            "messages": [
                {"role": "user", "content": "Hello, how are you?"}
            ],
            "model": "test-model",
            "stream": False,
            "max_tokens": 100
        }
    }


@pytest.fixture
def sample_broker_streaming_start_message(test_job_id):
    """Sample broker START message for streaming"""
    return {
        "type": "START", 
        "job_id": test_job_id,
        "payload": {
            "messages": [
                {"role": "user", "content": "Tell me a story"}
            ],
            "model": "test-model",
            "stream": True,
            "max_tokens": 200
        }
    }


@pytest.fixture
def sample_proof_request_message(test_completion_id):
    """Sample proof request message"""
    return {
        "type": "PROOF_REQUEST",
        "completion_id": test_completion_id
    }


@pytest.fixture
def mock_proof_blob():
    """Mock proof blob data"""
    return b"mock_proof_flatbuffer_data_" + os.urandom(64)


@pytest.fixture
def sample_hello_message():
    """Sample HELLO message that worker should send"""
    worker_id = str(uuid.uuid4())
    return {
        "type": "HELLO",
        "worker_id": worker_id,
        "models": ["test-model-1", "test-model-2"],
        "capacity": 2,
        "capabilities": {
            "compute_type": "test-gpu",
            "gpu_model": "TestGPU-16GB",
            "memory_gb": 16,
            "region": "test-region", 
            "max_context_window": 4096,
            "features": ["streaming", "pow_injection"],
            "quantization": ["fp16", "int8"]
        }
    }


@pytest.fixture
def sample_ack_message():
    """Sample ACK message from broker"""
    return {
        "type": "ACK",
        "heartbeat_interval_sec": 10,
        "status": "registered"
    }


@pytest.fixture
def sample_challenge_message():
    """Sample CHALLENGE message from broker"""
    return {
        "type": "CHALLENGE",
        "nonce": f"test-nonce-{uuid.uuid4()}",
        "timestamp": 1699123456
    }


@pytest.fixture
def expected_heartbeat_message():
    """Expected structure of heartbeat message"""
    return {
        "type": "HEARTBEAT",
        "busy": 0,
        "input_tokens_per_sec": 0.0,
        "output_tokens_per_sec": 0.0,
        "error_rate": 0.0,
        "queue_depth": 0
    }


@pytest.fixture
def temp_test_dir():
    """Create temporary directory for test files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def mock_miner_proxy_status():
    """Mock miner proxy status response"""
    return {
        "context": {
            "status": "active",
            "block_hash": "0" * 64,
            "difficulty": 1000000
        },
        "vdf": {
            "status": "running",
            "discriminant_size": 1024
        },
        "zmq": {
            "status": "listening",
            "port": 6000
        },
        "proxy": {
            "status": "active", 
            "active_requests": 2,
            "input_tokens_per_sec": 150.5,
            "output_tokens_per_sec": 45.2,
            "total_requests": 1523,
            "successful_requests": 1501,
            "failed_requests": 22
        }
    }


@pytest.fixture
def mock_models_response():
    """Mock /v1/models endpoint response"""
    return {
        "object": "list",
        "data": [
            {
                "id": "Qwen/Qwen3-8B",
                "object": "model",
                "created": 1699123456,
                "owned_by": "system"
            },
            {
                "id": "test-model-custom",
                "object": "model", 
                "created": 1699123457,
                "owned_by": "user"
            }
        ]
    }


class MockWebSocketMessage:
    """Mock WebSocket message for testing"""
    
    def __init__(self, msg_type, data=None):
        self.type = msg_type
        self.data = json.dumps(data) if data else ""


class MockWebSocketConnection:
    """Mock WebSocket connection for testing"""
    
    def __init__(self):
        self.messages_sent = []
        self.messages_to_receive = []
        self.closed = False
        self.exception_to_raise = None
        
    async def send_str(self, data):
        """Mock send_str method"""
        self.messages_sent.append(json.loads(data))
        
    async def close(self):
        """Mock close method"""
        self.closed = True
        
    def add_message_to_receive(self, message):
        """Add message that should be received"""
        self.messages_to_receive.append(message)
        
    async def __aiter__(self):
        """Mock async iteration over messages"""
        for msg in self.messages_to_receive:
            yield msg
        if self.exception_to_raise:
            raise self.exception_to_raise


@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection fixture"""
    return MockWebSocketConnection()


# Test markers for different test categories
def pytest_configure(config):
    """Configure custom pytest markers"""
    config.addinivalue_line("markers", "unit: Unit tests (fast, isolated)")
    config.addinivalue_line("markers", "integration: Integration tests (slower, with mocked services)")
    config.addinivalue_line("markers", "protocol: Protocol compliance tests")
    config.addinivalue_line("markers", "completion_id: Completion ID integrity tests")
    config.addinivalue_line("markers", "error_handling: Error handling and resilience tests")
    config.addinivalue_line("markers", "slow: Slow tests (timeouts, long operations)")


# Async test configuration
@pytest.fixture(scope="function")
def anyio_backend():
    """Configure anyio backend for async tests"""
    return "asyncio"