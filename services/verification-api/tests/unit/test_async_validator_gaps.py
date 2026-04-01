# SPDX-License-Identifier: Apache-2.0
import time
import threading

import pytest

from utils.proof import (
    ValidationType,
    ResponseValue,
    ValidationResponse,
    ValidationRequest,
)
from helpers.fb_builders import (
    build_block_validation_request,
)


# Lightweight fakes to avoid real ZMQ during unit tests
class _FakeSocket:
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


class _FakeContext:
    def socket(self, _):
        return _FakeSocket()
    def term(self):
        pass


class _FakeBroker:
    def __init__(self, *_a, **_k):
        self.submitted = []
        self.running = False
    def start(self):
        self.running = True
    def stop(self):
        self.running = False
    def submit(self, payload):
        self.submitted.append(payload)
        return True


@pytest.fixture()
def validator(monkeypatch):
    import zmq
    monkeypatch.setattr(zmq, "Context", lambda: _FakeContext())
    import main as main
    monkeypatch.setattr(main, "ZmqSendBroker", _FakeBroker)
    return main.AsyncValidator(pull_port=6030, push_host="127.0.0.1", push_port=7030)


def _h(seed):
    return (seed.to_bytes(4, "big") * 8)[:32]


def test_propagation_does_not_send_without_full_requested(validator):
    # A fails; B depends on A; B has no full_requested → no outbound send
    A, B = _h(1), _h(2)
    with validator.dependency_lock:
        validator.block_dependencies[A].add(B)
    # Simulate quick fail and propagate
    validator.set_phase_result(A, 'quick', ResponseValue.ResponseValue.Quick_Fail)
    validator.propagate_validation_failure(A)

    # B marked Full_Red but no response sent
    with validator.status_lock:
        assert validator.validation_status[B]['full'] == ResponseValue.ResponseValue.Full_Red
    assert len(validator.sender.submitted) == 0


def test_propagation_multilevel_chain(validator):
    # Chain A -> B -> C, only C is full_requested
    A, B, C = _h(10), _h(11), _h(12)
    with validator.dependency_lock:
        validator.block_dependencies[A].add(B)
        validator.block_dependencies[B].add(C)
    with validator.full_req_lock:
        validator.full_requested.add(C)

    validator.set_phase_result(A, 'quick', ResponseValue.ResponseValue.Quick_Fail)
    validator.propagate_validation_failure(A)

    with validator.status_lock:
        assert validator.validation_status[B]['full'] == ResponseValue.ResponseValue.Full_Red
        assert validator.validation_status[C]['full'] == ResponseValue.ResponseValue.Full_Red
    # Only C should have a response
    assert len(validator.sender.submitted) == 1
    payload = validator.sender.submitted[0]
    r = ValidationResponse.ValidationResponse.GetRootAs(payload, 0)
    got_hash = bytes(r.HashIdentifierAsNumpy().tolist())
    assert got_hash == C
    assert r.EnumResponse() == ResponseValue.ResponseValue.Full_Red


def test_enqueue_full_with_known_full_status_sends_immediately(validator):
    # Pre-mark hash full status and then enqueue a Full request → immediate send
    H = _h(20)
    with validator.status_lock:
        st = validator.validation_status.setdefault(H, {})
        st['full'] = ResponseValue.ResponseValue.Full_Red
    msg = build_block_validation_request(
        hash_id=H,
        prev_hash=_h(21),
        validation_type=ValidationType.ValidationType.Full,
    )
    validator.enqueue_request(msg)

    assert len(validator.sender.submitted) == 1
    r = ValidationResponse.ValidationResponse.GetRootAs(validator.sender.submitted[0], 0)
    assert bytes(r.HashIdentifierAsNumpy().tolist()) == H
    assert r.EnumResponse() == ResponseValue.ResponseValue.Full_Red


def test_quick_smell_ok_smell_fail_does_not_propagate(validator, monkeypatch):
    # A quick_smell returns Quick_OK_Smell_Fail; dependency B should NOT be propagated
    A, B = _h(30), _h(31)
    with validator.dependency_lock:
        validator.block_dependencies[A].add(B)

    # Build a Quick_Smell request for A
    raw = build_block_validation_request(
        hash_id=A,
        prev_hash=_h(29),
        validation_type=ValidationType.ValidationType.Quick_Smell,
    )
    data = {
        'hash_id': A,
        'validation_type': ValidationType.ValidationType.Quick_Smell,
        'request': ValidationRequest.ValidationRequest.GetRootAs(raw, 0),
        'raw_message': raw,
        'timestamp': time.time(),
        'retry_count': 0,
    }
    import proof.ResponseValue as RV
    class _V:
        # **_kwargs accepts target_override_hex from slice-11 share-mode
        # forwarding without forcing this test to track production signature
        # changes.
        def quick_verify_smell_test(self, _, **_kwargs):
            return RV.ResponseValue.Quick_OK_Smell_Fail

    before = len(validator.sender.submitted)
    validator.validate_quick_smell(data, _V())

    # One response for A; B untouched (no full status set)
    assert len(validator.sender.submitted) == before + 1
    with validator.status_lock:
        assert 'full' not in validator.validation_status.get(B, {})


def test_wait_for_quick_validation_timeout_returns_false(validator):
    H = _h(40)
    t0 = time.time()
    ok = validator.wait_for_quick_validation(H, timeout=0.05)
    dt = time.time() - t0
    assert not ok and dt >= 0.05


def test_send_error_response_does_not_vote(validator):
    """Local execution errors must not be turned into validator votes.

    Contract: send_error_response logs the failure and clears the pending
    event but does NOT update validation_status and does NOT submit a
    response on the broker. Quietly dropping is correct: the request
    layer sees no answer and lets upstream timeouts decide.
    """
    H = _h(50)
    # Create event by waiting once (registers H in validation_events)
    _ = validator.wait_for_quick_validation(H, timeout=0.01)
    with validator.events_lock:
        assert H in validator.validation_events

    validator.send_error_response(H, kind='quick')

    # No phase set — no spurious vote
    with validator.status_lock:
        assert H not in validator.validation_status
    # Event cleared
    with validator.events_lock:
        assert H not in validator.validation_events
    # No outbound response
    assert validator.sender.submitted == []


def test_is_already_processed(validator):
    H = _h(60)
    # Not processed if only quick set
    validator.set_phase_result(H, 'quick', ResponseValue.ResponseValue.Quick_OK)
    assert validator.is_already_processed(H) is False
    # Processed when full present
    validator.set_phase_result(H, 'full', ResponseValue.ResponseValue.Full_Green)
    assert validator.is_already_processed(H) is True


def test_unknown_validation_type_is_ignored(validator):
    H = _h(70)
    # Build request with an unknown validation type value
    raw = build_block_validation_request(
        hash_id=H,
        prev_hash=_h(71),
        validation_type=255,  # not in enum, but within uint8 range
    )
    validator.enqueue_request(raw)
    # No queues should receive items; no responses sent
    assert validator.quick_queue.qsize() == 0
    assert validator.quick_smell_queue.qsize() == 0
    assert validator.full_queue.qsize() == 0
    assert validator.model_queue.qsize() == 0
    assert len(validator.sender.submitted) == 0

