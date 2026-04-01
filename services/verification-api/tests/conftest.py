# SPDX-License-Identifier: Apache-2.0
"""
Test configuration and fixtures for verification-api tests
Handles FlatBuffers imports, ZMQ mocking, and test utilities
"""

import sys
import os
import pytest
import asyncio
import contextlib
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, AsyncMock, MagicMock
from typing import Optional, Dict, Any, List
import struct

# Add src directory to path
test_dir = Path(__file__).parent
src_dir = test_dir.parent / "src"
project_root = test_dir.parent.parent.parent

sys.path.insert(0, str(src_dir))
sys.path.insert(0, str(project_root))

# ---------------------------------------------------------------------------
# Early lightweight stubs to avoid importing heavyweight libs (e.g., torch)
# before tests have a chance to install mocks. This prevents long GPU probing
# or binary loads in CI when importing src/main.py.
# ---------------------------------------------------------------------------
def _early_stub_torch():
    if 'torch' in sys.modules:
        return
    import types as _types

    torch_mod = _types.ModuleType('torch')

    class _DType:
        def __init__(self, name):
            self.name = name
        def __repr__(self):
            return f"<torch.dtype {self.name}>"
        def __hash__(self):
            return hash(self.name)
        def __eq__(self, other):
            return isinstance(other, _DType) and other.name == self.name

    # dtypes and constants used by code/constants
    torch_mod.float16 = _DType('float16')
    torch_mod.bfloat16 = _DType('bfloat16')
    torch_mod.float32 = _DType('float32')
    torch_mod.float64 = _DType('float64')
    torch_mod.int8 = _DType('int8')
    torch_mod.int16 = _DType('int16')
    torch_mod.int32 = _DType('int32')
    torch_mod.int64 = _DType('int64')
    torch_mod.uint8 = _DType('uint8')
    torch_mod.bool = _DType('bool')
    torch_mod.pi = 3.141592653589793

    # minimal distributions API
    dists_mod = _types.ModuleType('torch.distributions')
    class _StudentT:
        def __init__(self, df, loc=0.0, scale=1.0):
            self.df = df
            self.loc = loc
            self.scale = scale
    dists_mod.StudentT = _StudentT
    normal_mod = _types.ModuleType('torch.distributions.normal')
    class _Normal:
        def __init__(self, loc, scale):
            self.loc = loc
            self.scale = scale
    normal_mod.Normal = _Normal

    sys.modules['torch'] = torch_mod
    sys.modules['torch.distributions'] = dists_mod
    sys.modules['torch.distributions.normal'] = normal_mod
    torch_mod.distributions = dists_mod
    dists_mod.normal = normal_mod

_early_stub_torch()

# Provide a lightweight ZeroMQ stub if pyzmq is unavailable
def _early_stub_zmq():
    import types as _types
    import sys as _sys
    import time as _time
    import threading as _threading
    from queue import Queue, Empty as _QEmpty
    if 'zmq' in _sys.modules:
        return
    zmq_mod = _types.ModuleType('zmq')
    # Socket types and constants used
    zmq_mod.PULL = 1
    zmq_mod.PUSH = 2
    zmq_mod.SNDHWM = 23
    zmq_mod.LINGER = 17
    zmq_mod.DONTWAIT = 1
    zmq_mod.EAGAIN = 11
    zmq_mod.POLLIN = 1

    class Again(Exception):
        pass
    zmq_mod.Again = Again

    # Global simple endpoint registry for in-process message routing
    _REGISTRY = {}
    _REG_LOCK = _threading.RLock()

    def _norm(endpoint: str) -> str:
        try:
            if endpoint.startswith("tcp://"):
                # Normalize to tcp://:<port>
                port = endpoint.rsplit(":", 1)[-1]
                return f"tcp://:{port}"
        except Exception:
            pass
        return endpoint

    class _FakeSocket:
        def __init__(self, ctx, sock_type):
            self._ctx = ctx
            self._type = sock_type
            self._opts = {}
            self._bound_ep = None
            self._connected_eps = []
            self._recv_q: Queue[bytes] = Queue()
            self._closed = False

        def setsockopt(self, opt, val):
            self._opts[opt] = val

        def bind(self, endpoint: str):
            self._bound_ep = _norm(endpoint)
            with _REG_LOCK:
                _REGISTRY.setdefault(self._bound_ep, []).append(self)

        def connect(self, endpoint: str):
            # PUSH will connect to PULL, but we don't type-enforce here
            self._connected_eps.append(_norm(endpoint))

        def poll(self, timeout_ms: int):
            # Ready to read?
            if not self._recv_q.empty():
                return 1
            if timeout_ms <= 0:
                return 0
            # Busy-wait with short sleep windows
            deadline = _time.time() + (timeout_ms / 1000.0)
            while _time.time() < deadline:
                if not self._recv_q.empty():
                    return 1
                _time.sleep(0.001)
            return 0

        def recv(self):
            try:
                return self._recv_q.get_nowait()
            except _QEmpty:
                raise Again()

        def send(self, item: bytes, flags=0):
            # Route to the first bound socket on any connected endpoint
            delivered = False
            with _REG_LOCK:
                for ep in list(self._connected_eps):
                    targets = _REGISTRY.get(ep, [])
                    if targets:
                        # Simple round-robin: first target
                        targets[0]._recv_q.put(item)
                        delivered = True
                        break
            if not delivered:
                # No remote bound; emulate EAGAIN in non-blocking mode
                if flags & zmq_mod.DONTWAIT:
                    raise Again()
                # Best-effort drop
                return False
            return True

        def close(self, *_):
            self._closed = True
            # Remove from registry if bound
            if self._bound_ep is not None:
                with _REG_LOCK:
                    arr = _REGISTRY.get(self._bound_ep, [])
                    _REGISTRY[self._bound_ep] = [s for s in arr if s is not self]

    class _FakeContext:
        def __init__(self, *_a, **_k):
            pass
        def socket(self, sock_type):
            return _FakeSocket(self, sock_type)
        def term(self):
            pass
        def destroy(self, linger=0):
            pass

    def _Context(*_a, **_k):
        return _FakeContext()

    class _Poller:
        def __init__(self):
            self._socks = []
        def register(self, sock, *_):
            self._socks.append(sock)
        def unregister(self, sock):
            self._socks = [s for s in self._socks if s is not sock]
        def poll(self, timeout_ms):
            # Check once; if nothing, wait
            start = _time.time()
            while True:
                ready = [(s, zmq_mod.POLLIN) for s in self._socks if s._recv_q.qsize() > 0]
                if ready:
                    return ready
                if (timeout_ms is not None) and ((time_left := (timeout_ms/1000.0) - (_time.time() - start)) <= 0):
                    return []
                _time.sleep(0.001)

    zmq_mod.Context = _Context
    zmq_mod.Poller = _Poller
    _sys.modules['zmq'] = zmq_mod

_early_stub_zmq()

# Provide a minimal socket.socket stub to allocate fake ephemeral ports
def _early_stub_socket():
    import sys as _sys
    try:
        import socket as _socket
    except Exception:
        return
    # Replace only the constructor; keep constants from real module
    if getattr(_socket, "__FAKE_BOUND__", False):
        return
    _socket.__FAKE_BOUND__ = True

    _PORT_COUNTER = {"n": 55000}

    class _FakeSock:
        def __init__(self, *a, **k):
            self._bound = False
            self._addr = ("127.0.0.1", 0)
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            self.close()
        def bind(self, addr):
            host, port = addr
            if port == 0:
                _PORT_COUNTER["n"] += 1
                port = _PORT_COUNTER["n"]
            self._addr = (host, port)
            self._bound = True
        def listen(self, *_):
            pass
        def getsockname(self):
            return self._addr
        def settimeout(self, *_):
            pass
        def close(self):
            pass

    def _fake_socket(*a, **k):
        return _FakeSock()

    try:
        _socket.socket = _fake_socket
    except Exception:
        # Best effort; ignore if cannot replace
        pass

_early_stub_socket()

# Provide package-style aliases so tests can import via
# `services.verification_api.src.*` while running from repo root.
try:
    import types as _types
    import importlib as _importlib
    if 'services' not in sys.modules:
        _svc = _types.ModuleType('services')
        _svc.__path__ = []  # mark as package
        sys.modules['services'] = _svc
    if 'services.verification_api' not in sys.modules:
        _vapi = _types.ModuleType('services.verification_api')
        _vapi.__path__ = [str(test_dir.parent)]  # package path anchor
        sys.modules['services.verification_api'] = _vapi
    if 'services.verification_api.src' not in sys.modules:
        _srcpkg = _types.ModuleType('services.verification_api.src')
        _srcpkg.__path__ = [str(src_dir)]
        sys.modules['services.verification_api.src'] = _srcpkg

    # Do not import main eagerly here to avoid heavy imports before mocks.
    # The above __path__ settings allow `import services.verification_api.src.main`
    # to resolve naturally when tests import it.
except Exception:
    # Aliasing is best-effort; tests fall back to direct imports
    pass

# Environment setup
os.environ['TEST_MODE'] = 'true'
os.environ['CUDA_VISIBLE_DEVICES'] = ''  # Disable GPU for tests

# Configure logging
import logging
logging.getLogger().setLevel(logging.WARNING)

# ============================================================================
# Mock Modules Setup
# ============================================================================

def setup_mock_modules():
    """Create mocks only when real modules are unavailable.

    - Always stub torch to avoid GPU detection overhead.
    - Prefer the real utils.proof shim if importable; otherwise provide minimal mocks.
    - Provide a lightweight proof_verifier stub so tests can monkeypatch it
      without importing the heavyweight implementation.
    """

    # Torch: provide a lightweight, importable stub (including submodules)
    if 'torch' not in sys.modules:
        import types as _types

        torch_mod = _types.ModuleType('torch')

        # dtypes used as dictionary keys in constants
        class _DType:  # simple, hashable placeholder
            def __init__(self, name):
                self.name = name
            def __repr__(self):
                return f"<torch.dtype {self.name}>"
            def __hash__(self):
                return hash(self.name)
            def __eq__(self, other):
                return isinstance(other, _DType) and other.name == self.name

        torch_mod.float16 = _DType('float16')
        torch_mod.bfloat16 = _DType('bfloat16')
        torch_mod.float32 = _DType('float32')
        torch_mod.float64 = _DType('float64')
        torch_mod.int8 = _DType('int8')
        torch_mod.int16 = _DType('int16')
        torch_mod.int32 = _DType('int32')
        torch_mod.int64 = _DType('int64')
        torch_mod.uint8 = _DType('uint8')
        torch_mod.bool = _DType('bool')

        # minimal math constants/APIs referenced
        torch_mod.pi = 3.141592653589793
        torch_mod.device = Mock(return_value='cpu')

        # cuda shim
        cuda_mod = _types.SimpleNamespace(
            is_available=Mock(return_value=False),
            device_count=Mock(return_value=0),
        )
        torch_mod.cuda = cuda_mod

        # torch.distributions and submodules
        dists_mod = _types.ModuleType('torch.distributions')

        class _Normal:
            def __init__(self, loc, scale):
                self.loc = loc
                self.scale = scale

        class _StudentT:
            def __init__(self, df, loc=0.0, scale=1.0):
                self.df = df
                self.loc = loc
                self.scale = scale

        # expose on distributions module
        dists_mod.StudentT = _StudentT

        # torch.distributions.normal submodule
        normal_mod = _types.ModuleType('torch.distributions.normal')
        normal_mod.Normal = _Normal

        # wire everything into sys.modules and as attributes
        sys.modules['torch'] = torch_mod
        sys.modules['torch.distributions'] = dists_mod
        sys.modules['torch.distributions.normal'] = normal_mod
        # make them reachable via attributes too
        torch_mod.distributions = dists_mod
        dists_mod.normal = normal_mod

    # utils.proof: use the real shim if available
    have_real_proof = False
    try:
        import utils.proof as _proof  # noqa: F401
        have_real_proof = True
    except Exception:
        have_real_proof = False

    if not have_real_proof:
        # Minimal mocks for parsing-only scenarios (builders come from real shim when present)
        if 'utils' not in sys.modules:
            sys.modules['utils'] = MagicMock()
        if 'utils.proof' not in sys.modules:
            sys.modules['utils.proof'] = MagicMock()

        mock_proof = sys.modules['utils.proof']

        class MockValidationRequest:
            def __init__(self):
                self.hash_id = b"test_hash_000000"
                self.validation_type = 1  # Quick

            @classmethod
            def GetRootAs(cls, buf, offset):
                return cls()

            def HashId(self):
                return self.hash_id

            def HashIdAsNumpy(self):
                return self.hash_id

            def RequestType(self):
                return 1  # ValidationUnion.BlockValidation

            def Request(self):
                return MockBlockValidation()

        class MockBlockValidation:
            def __init__(self):
                self.hash_field = b"0" * 32
                self.prev_block_hash = b"0" * 32

            def Hash(self):
                return self.hash_field

            def HashAsNumpy(self):
                return self.hash_field

            def PrevBlockHash(self):
                return self.prev_block_hash

            def PrevBlockHashAsNumpy(self):
                return self.prev_block_hash

        class MockModelValidation:
            def __init__(self):
                self.model_hash = b"model_hash_000"
                self.model_name = b"test_model"

        class MockValidationResponse:
            def __init__(self):
                pass

        mock_proof.ValidationRequest = MockValidationRequest
        mock_proof.BlockValidation = MockBlockValidation
        mock_proof.ModelValidation = MockModelValidation
        mock_proof.ValidationResponse = MockValidationResponse

        # Minimal enums
        mock_proof.ValidationType_Quick = 1
        mock_proof.ValidationType_Quick_Smell = 2
        mock_proof.ValidationType_Full = 3
        mock_proof.ValidationType_Model = 4
        mock_proof.ResponseValue_Quick_OK = 1
        mock_proof.ResponseValue_Quick_Fail = 2
        mock_proof.ResponseValue_Quick_OK_Smell_OK = 3
        mock_proof.ResponseValue_Quick_OK_Smell_Fail = 4
        mock_proof.ResponseValue_Quick_Fail_Smell_Fail = 5
        mock_proof.ResponseValue_Full_Green = 6
        mock_proof.ResponseValue_Full_Amber = 7
        mock_proof.ResponseValue_Full_Red = 8
        mock_proof.ResponseValue_Model_OK = 9
        mock_proof.ResponseValue_Model_Fail = 10

        # Also expose a `proof` package mirror with a ResponseValue submodule so
        # tests can import `proof.ResponseValue` for monkeypatching.
        import types as _types
        if 'proof' not in sys.modules:
            _pkg = _types.ModuleType('proof')
            _pkg.__path__ = []  # mark as package
            sys.modules['proof'] = _pkg
        # Create/replace proof.ResponseValue submodule
        _rv_mod = _types.ModuleType('proof.ResponseValue')
        # Provide a nested `ResponseValue` attr mirroring the mock above
        class _RV:
            class ResponseValue:
                Quick_OK = mock_proof.ResponseValue_Quick_OK
                Quick_Fail = mock_proof.ResponseValue_Quick_Fail
                Quick_OK_Smell_OK = mock_proof.ResponseValue_Quick_OK_Smell_OK
                Quick_OK_Smell_Fail = mock_proof.ResponseValue_Quick_OK_Smell_Fail
                Quick_Fail_Smell_Fail = mock_proof.ResponseValue_Quick_Fail_Smell_Fail
                Full_Green = mock_proof.ResponseValue_Full_Green
                Full_Amber = mock_proof.ResponseValue_Full_Amber
                Full_Red = mock_proof.ResponseValue_Full_Red
                Model_OK = mock_proof.ResponseValue_Model_OK
                Model_Fail = mock_proof.ResponseValue_Model_Fail
        _rv_mod.ResponseValue = _RV.ResponseValue
        sys.modules['proof.ResponseValue'] = _rv_mod

    # Provide a lightweight proof_verifier module for tests that import it directly
    if 'proof_verifier' not in sys.modules:
        try:
            from utils.proof import ResponseValue as _RV
        except Exception:
            class _RV:  # very minimal fallback
                class ResponseValue:
                    Quick_OK = 0
                    Quick_OK_Smell_OK = 2

        import types
        _pv = types.ModuleType('proof_verifier')

        class _StubProofVerifier:
            def quick_verify(self, _buf, **_kwargs):
                return _RV.ResponseValue.Quick_OK

            def quick_verify_smell_test(self, _buf, **_kwargs):
                return _RV.ResponseValue.Quick_OK_Smell_OK

            def full_verify(self, _buf, **_kwargs):
                return "GREEN"

        _pv.ProofVerifier = _StubProofVerifier
        _pv.mca_install = lambda *a, **k: None
        _pv.mca_set_enabled = lambda *a, **k: None
        _pv.mca_set_params = lambda *a, **k: None
        _pv.mca_active = lambda *a, **k: contextlib.nullcontext()
        sys.modules['proof_verifier'] = _pv

# Setup mocks on import (torch is already stubbed by the early stub)
setup_mock_modules()

# Ensure a `proof` package with `ResponseValue` submodule exists for tests that
# import `proof.ResponseValue` directly, regardless of whether a real shim is present.
try:
    import types as _types
    if 'proof' not in sys.modules:
        _pkg = _types.ModuleType('proof')
        _pkg.__path__ = []
        sys.modules['proof'] = _pkg
    if 'proof.ResponseValue' not in sys.modules:
        _rv_mod = _types.ModuleType('proof.ResponseValue')
        try:
            from utils.proof import ResponseValue as _RealRV
            _rv_mod.ResponseValue = _RealRV.ResponseValue
        except Exception:
            # Fall back to a minimal shape if real shim is unavailable; will be
            # overwritten by earlier mock when applicable.
            class _RV:
                class ResponseValue:
                    Quick_OK = 0
                    Quick_Fail = 1
                    Quick_OK_Smell_OK = 2
                    Quick_OK_Smell_Fail = 3
                    Quick_Fail_Smell_Fail = 4
                    Full_Green = 5
                    Full_Amber = 6
                    Full_Red = 7
                    Model_OK = 8
                    Model_Fail = 9
            _rv_mod.ResponseValue = _RV.ResponseValue
        sys.modules['proof.ResponseValue'] = _rv_mod
except Exception:
    pass

# ============================================================================
# Pytest Fixtures
# ============================================================================

@pytest.fixture
def mock_zmq_context():
    """Mock ZMQ context that doesn't bind real sockets"""
    context = MagicMock()
    
    # Mock socket
    socket = MagicMock()
    socket.bind = Mock()
    socket.connect = Mock()
    socket.setsockopt = Mock()
    socket.close = Mock()
    socket.poll = Mock(return_value=False)
    socket.recv = Mock(return_value=b"mock_message")
    socket.send = Mock(return_value=True)
    
    context.socket = Mock(return_value=socket)
    
    # Patch zmq module
    import zmq
    original_context = zmq.Context
    zmq.Context = Mock(return_value=context)
    
    yield context
    
    # Restore
    zmq.Context = original_context

@pytest.fixture
def mock_proof_verifier(monkeypatch):
    """Mock ProofVerifier with configurable outcomes"""
    
    class MockProofVerifier:
        def __init__(self):
            self.quick_verify_result = "Quick_OK"
            self.smell_test_result = "Quick_OK_Smell_OK"
            self.full_verify_result = "GREEN"
            self.call_history = []
        
        def quick_verify(self, *args, **kwargs):
            self.call_history.append(('quick_verify', args, kwargs))
            return self.quick_verify_result
        
        def quick_verify_smell_test(self, *args, **kwargs):
            self.call_history.append(('quick_verify_smell_test', args, kwargs))
            return self.smell_test_result
        
        def full_verify(self, *args, **kwargs):
            self.call_history.append(('full_verify', args, kwargs))
            return self.full_verify_result
        
        def set_results(self, quick=None, smell=None, full=None):
            if quick: self.quick_verify_result = quick
            if smell: self.smell_test_result = smell
            if full: self.full_verify_result = full
    
    mock_verifier = MockProofVerifier()
    monkeypatch.setattr("proof_verifier.ProofVerifier", lambda: mock_verifier)
    return mock_verifier

@pytest.fixture
def mock_model_verifier(monkeypatch):
    """Mock ModelVerifier"""
    
    class MockModelVerifier:
        def __init__(self):
            self.validate_result = True
            self.call_history = []
        
        def validate(self, *args, **kwargs):
            self.call_history.append(('validate', args, kwargs))
            return self.validate_result
        
        def model_validation(self, *args, **kwargs):
            self.call_history.append(('model_validation', args, kwargs))
            return self.validate_result
    
    mock_verifier = MockModelVerifier()
    monkeypatch.setattr("model_verifier.ModelVerifier", lambda: mock_verifier)
    return mock_verifier

@pytest.fixture
def mock_zmq_send_broker(monkeypatch):
    """Mock ZmqSendBroker to capture sent messages"""
    
    class MockSendBroker:
        def __init__(self, *args, **kwargs):
            self.sent_messages = []
            self.running = True
        
        def submit(self, message: bytes) -> bool:
            self.sent_messages.append(message)
            return True
        
        def start(self):
            pass
        
        def stop(self):
            self.running = False
    
    mock_broker = MockSendBroker()
    monkeypatch.setattr("zmq_send_broker.ZmqSendBroker", lambda *args, **kwargs: mock_broker)
    return mock_broker

@pytest.fixture
async def ephemeral_ports():
    """Get ephemeral ports for testing"""
    import socket
    
    def get_free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port
    
    return {
        'pull_port': get_free_port(),
        'push_port': get_free_port()
    }

@pytest.fixture
def flatbuffer_builder():
    """Helper to build FlatBuffers messages"""
    
    class FlatBufferBuilder:
        @staticmethod
        def build_validation_request(
            hash_id: str = "test_hash",
            validation_type: int = 1,  # Quick
            prev_hash: Optional[str] = None
        ) -> bytes:
            """Build a mock ValidationRequest"""
            # Simple mock implementation
            # In real tests, use actual FlatBuffers builder
            data = {
                'hash_id': hash_id.encode() if isinstance(hash_id, str) else hash_id,
                'type': validation_type,
                'prev_hash': prev_hash.encode() if prev_hash else None
            }
            return str(data).encode()
        
        @staticmethod
        def parse_validation_response(data: bytes) -> Dict[str, Any]:
            """Parse a ValidationResponse"""
            # Mock implementation
            return {
                'hash': 'parsed_hash',
                'result': 'Quick_OK'
            }
    
    return FlatBufferBuilder()

@pytest.fixture
def temp_test_dir():
    """Create a temporary directory for test files"""
    temp_dir = tempfile.mkdtemp(prefix="verification_test_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture
def async_validator_config():
    """Default configuration for AsyncValidator"""
    return {
        'pull_port': 5555,
        'push_host': 'localhost',
        'push_port': 5556,
        'quick_queue_size': 100,
        'quick_smell_queue_size': 100,
        'full_queue_size': 50,
        'model_queue_size': 20
    }

# ============================================================================
# Test Utilities
# ============================================================================

class AsyncContextManager:
    """Helper for testing async context managers"""
    
    def __init__(self, return_value=None):
        self.return_value = return_value
        self.entered = False
        self.exited = False
    
    async def __aenter__(self):
        self.entered = True
        return self.return_value
    
    async def __aexit__(self, *args):
        self.exited = True

def create_mock_request(
    hash_id: str = "test_hash",
    validation_type: str = "Quick",
    prev_hash: Optional[str] = None
) -> Dict[str, Any]:
    """Create a mock validation request"""
    type_map = {
        'Quick': 1,
        'Quick_Smell': 2,
        'Full': 3,
        'Model': 4
    }
    
    return {
        'hash_id': hash_id,
        'validation_type': type_map.get(validation_type, 1),
        'prev_hash': prev_hash,
        'timestamp': 0
    }

async def wait_for_condition(
    condition_func,
    timeout: float = 1.0,
    interval: float = 0.01
) -> bool:
    """Wait for a condition to become true"""
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        if condition_func():
            return True
        await asyncio.sleep(interval)
    return False

# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Configure pytest"""
    config.addinivalue_line(
        "markers", 
        "unit: Unit tests that don't require external services"
    )
    config.addinivalue_line(
        "markers",
        "integration: Integration tests that may require ZMQ or other services"
    )
    config.addinivalue_line(
        "markers",
        "e2e: End-to-end tests with full system"
    )
    config.addinivalue_line(
        "markers",
        "fp8: Tests covering FP8 model loading / replay paths"
    )

# NOTE:
# pytest-asyncio (v0.21+) running under pytest>=8 defaults to strict mode and
# manages its own event loop lifecycle. Defining a custom session-scoped
# `event_loop` fixture can interfere with teardown and occasionally cause the
# test runner to hang in CI while waiting for loop shutdown. We therefore rely
# on the plugin-managed loop and avoid defining our own `event_loop` fixture.
# If a test needs an event loop, it should use pytest-asyncio's fixtures.
