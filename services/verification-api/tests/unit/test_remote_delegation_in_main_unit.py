# SPDX-License-Identifier: Apache-2.0
import time

from utils.proof import (
    ValidationRequest,
    ValidationResponse,
    ResponseValue,
)
from helpers.fb_builders import (
    build_model_validation_request,
    build_block_validation_request,
)


class _FakeBroker:
    def __init__(self, *_, **__):
        self.submitted = []
        self.running = False
    def start(self):
        self.running = True
    def stop(self):
        self.running = False
    def submit(self, payload: bytes) -> bool:
        self.submitted.append(payload)
        return True


def _hash_bytes(seed: int) -> bytes:
    return (seed.to_bytes(4, "big") * 8)[:32]


def test_model_remote_takes_precedence(monkeypatch):
    import zmq
    # Patch ZMQ Context to avoid binding real sockets
    class _Ctx:
        def socket(self, _):
            class _S:
                def bind(self, *_):
                    pass
                def setsockopt(self, *_):
                    pass
                def poll(self, *_):
                    return 0
                def recv(self):
                    raise RuntimeError
                def close(self, *_):
                    pass
            return _S()
        def term(self):
            pass
    monkeypatch.setattr(zmq, "Context", lambda: _Ctx())

    # Import main and patch broker
    import main as m
    monkeypatch.setattr(m, "ZmqSendBroker", _FakeBroker)

    # Enable remote delegation and stub delegate
    m.REMOTE_VERIFY_ENABLED = True
    m.REMOTE_VERIFY_BASE_URL = "https://attestor"

    # Mock the remote_delegate module functions
    def mock_verify_model_remote(vreq_bytes, base_url, api_key, timeout):
        return ResponseValue.ResponseValue.Model_OK
    
    # Create a mock module object
    import types
    mock_module = types.ModuleType('remote_delegate')
    mock_module.verify_model_remote = mock_verify_model_remote
    m.remote_delegate = mock_module

    v = m.AsyncValidator(pull_port=6099, push_host="127.0.0.1", push_port=7099)
    # Local model validator should NOT be called
    called = {"n": 0}
    def _should_not_be_called(_):
        called["n"] += 1
        return False
    v.model_validator.validate = _should_not_be_called

    h = _hash_bytes(777)
    raw = build_model_validation_request(hash_id=h)
    data = {
        'hash_id': h,
        'validation_type': None,
        'request': ValidationRequest.ValidationRequest.GetRootAs(raw, 0),
        'raw_message': raw,
        'timestamp': time.time(),
        'retry_count': 0,
    }
    v.validate_model(data)
    # Expect one response and local validator not touched
    assert len(v.sender.submitted) == 1
    r = ValidationResponse.ValidationResponse.GetRootAs(v.sender.submitted[0], 0)
    assert r.EnumResponse() == ResponseValue.ResponseValue.Model_OK
    assert called["n"] == 0


def test_full_remote_short_circuits_without_quick(monkeypatch):
    import zmq
    class _Ctx:
        def socket(self, _):
            class _S:
                def bind(self, *_):
                    pass
                def setsockopt(self, *_):
                    pass
                def poll(self, *_):
                    return 0
                def recv(self):
                    raise RuntimeError
                def close(self, *_):
                    pass
            return _S()
        def term(self):
            pass
    monkeypatch.setattr(zmq, "Context", lambda: _Ctx())

    import main as m
    monkeypatch.setattr(m, "ZmqSendBroker", _FakeBroker)

    m.REMOTE_VERIFY_ENABLED = True
    m.REMOTE_VERIFY_BASE_URL = "https://attestor"

    # Mock the remote_delegate module functions
    def mock_verify_full_remote(vreq_bytes, base_url, api_key, timeout):
        return ResponseValue.ResponseValue.Full_Amber
    
    # Create a mock module object
    import types
    mock_module = types.ModuleType('remote_delegate')
    mock_module.verify_full_remote = mock_verify_full_remote
    m.remote_delegate = mock_module

    v = m.AsyncValidator(pull_port=6100, push_host="127.0.0.1", push_port=7100)
    
    # Start the sender broker
    v.sender.start()

    # Enqueue a Full request; worker should handle via remote immediately
    h = _hash_bytes(42)
    raw = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(41), validation_type=2)  # 2 = Full
    v.enqueue_request(raw)

    # Run one full worker iteration in a short-lived thread
    v.running = True
    import threading
    t = threading.Thread(target=v.process_full_validations, daemon=True)
    t.start()
    time.sleep(0.2)
    v.running = False
    t.join(timeout=1.0)
    
    # Stop the sender broker
    v.sender.stop()

    assert len(v.sender.submitted) >= 1
    r = ValidationResponse.ValidationResponse.GetRootAs(v.sender.submitted[0], 0)
    assert r.EnumResponse() == ResponseValue.ResponseValue.Full_Amber

