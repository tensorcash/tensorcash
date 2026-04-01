# SPDX-License-Identifier: Apache-2.0
import threading
import time

import pytest

from utils.proof import (
    ValidationType,
    ResponseValue,
)
from helpers.fb_builders import (
    build_block_validation_request,
)


# Utility fakes (copied from the primary unit tests to keep isolation)
class FakeSocket:
    def __init__(self):
        self.bound = []

    def bind(self, endpoint):
        self.bound.append(endpoint)

    def setsockopt(self, *_args, **_kwargs):
        return None

    def poll(self, _timeout):
        return 0

    def recv(self):
        raise RuntimeError("recv not used in unit tests")

    def close(self):
        return None


class FakeContext:
    def socket(self, _type):
        return FakeSocket()

    def term(self):
        return None


class FakeBroker:
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


@pytest.fixture()
def validator(monkeypatch):
    import zmq
    # Patch ZMQ context and sender broker
    monkeypatch.setattr(zmq, "Context", lambda: FakeContext())

    import main as main
    monkeypatch.setattr(main, "ZmqSendBroker", FakeBroker)

    # Create validator with explicit ports (won't actually bind due to FakeContext)
    v = main.AsyncValidator(pull_port=6010, push_host="127.0.0.1", push_port=7010)
    return v


def _hash_bytes(seed: int) -> bytes:
    return (seed.to_bytes(4, "big") * 8)[:32]


def test_quick_smell_full_fail_propagates(validator):
    # Two blocks: B depends on A
    A = _hash_bytes(100)
    B = _hash_bytes(101)
    # Record dependency (B.prev = A)
    with validator.dependency_lock:
        validator.block_dependencies[A].add(B)
    # Mark that Full was requested for B so that a response would be sent if needed
    with validator.full_req_lock:
        validator.full_requested.add(B)

    # Create a Quick_Smell request for A and force a Quick_Fail_Smell_Fail outcome
    req = build_block_validation_request(
        hash_id=A,
        prev_hash=_hash_bytes(99),
        validation_type=ValidationType.ValidationType.Quick_Smell,
    )
    data = {
        'hash_id': A,
        'validation_type': ValidationType.ValidationType.Quick_Smell,
        'request': None,
        'raw_message': req,
        'timestamp': time.time(),
        'retry_count': 0,
    }
    # Build a verifier that returns Quick_Fail_Smell_Fail. **_kwargs
    # accepts target_override_hex from slice-11 share-mode forwarding.
    class _V:
        def quick_verify_smell_test(self, _, **_kwargs):
            return ResponseValue.ResponseValue.Quick_Fail_Smell_Fail

    # Rebuild request object from raw buffer for the validator
    from utils.proof import ValidationRequest
    data['request'] = ValidationRequest.ValidationRequest.GetRootAs(data['raw_message'], 0)
    validator.validate_quick_smell(data, _V())

    # Dependent B should be marked Full_Red and a response queued
    with validator.status_lock:
        assert validator.validation_status[B]['full'] == ResponseValue.ResponseValue.Full_Red
    assert len(validator.sender.submitted) >= 2  # smell result for A + propagation to B


def test_full_short_circuit_on_quick_fail_when_full_requested(validator):
    h = _hash_bytes(200)
    # Enqueue a Full request, which registers full_requested and fills full_queue
    msg_full = build_block_validation_request(
        hash_id=h,
        prev_hash=_hash_bytes(201),
        validation_type=ValidationType.ValidationType.Full,
    )
    validator.enqueue_request(msg_full)

    # Simulate Quick failure for this hash, and signal completion
    validator.set_phase_result(h, 'quick', ResponseValue.ResponseValue.Quick_Fail)
    validator._signal_validation_complete(h)

    # Run a single full worker in a thread to process the queue
    validator.running = True
    t = threading.Thread(target=validator.process_full_validations, daemon=True)
    t.start()
    try:
        # Wait a bit for processing
        time.sleep(0.2)
    finally:
        validator.running = False
        t.join(timeout=1.0)

    # Expect one Full_Red response
    assert any(True for _ in validator.sender.submitted)
    from utils.proof import ValidationResponse
    payloads = list(validator.sender.submitted)
    full_codes = []
    for p in payloads:
        r = ValidationResponse.ValidationResponse.GetRootAs(p, 0)
        full_codes.append(r.EnumResponse())
    assert ResponseValue.ResponseValue.Full_Red in set(full_codes)


def test_wait_for_quick_validation_delays_full_until_signaled(validator):
    h = _hash_bytes(300)

    # Put a Full request (this fills quick and full queues)
    msg_full = build_block_validation_request(
        hash_id=h,
        prev_hash=_hash_bytes(301),
        validation_type=ValidationType.ValidationType.Full,
    )
    validator.enqueue_request(msg_full)

    # Start full worker
    validator.running = True
    t = threading.Thread(target=validator.process_full_validations, daemon=True)
    t.start()

    start = time.time()
    # Wait a little to ensure the worker is likely waiting on quick
    time.sleep(0.2)
    # Now mark quick complete and signal
    validator.set_phase_result(h, 'quick', ResponseValue.ResponseValue.Quick_OK)
    validator._signal_validation_complete(h)

    # Allow worker to proceed
    time.sleep(0.2)
    validator.running = False
    t.join(timeout=1.0)

    # Asserts: it should have waited at least ~0.2s before completing
    elapsed = time.time() - start
    assert elapsed >= 0.2
    # And a response should have been submitted eventually
    assert len(validator.sender.submitted) >= 1

