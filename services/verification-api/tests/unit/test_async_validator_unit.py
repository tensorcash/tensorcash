# SPDX-License-Identifier: Apache-2.0
import os
import threading
import time
import types

import pytest


from utils.proof import (
    ValidationType,
    ResponseValue,
)
from helpers.fb_builders import (
    build_block_validation_request,
    build_model_validation_request,
)


# Utility fakes
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
    v = main.AsyncValidator(pull_port=6009, push_host="127.0.0.1", push_port=7009)
    return v


def _hash_bytes(seed: int) -> bytes:
    return (seed.to_bytes(4, "big") * 8)[:32]


def test_enqueue_routes_quick(validator):
    msg = build_block_validation_request(
        hash_id=_hash_bytes(1),
        prev_hash=_hash_bytes(100),
        validation_type=ValidationType.ValidationType.Quick,
    )
    validator.enqueue_request(msg)
    assert validator.quick_queue.qsize() == 1
    assert validator.quick_smell_queue.qsize() == 0
    assert validator.full_queue.qsize() == 0
    assert validator.model_queue.qsize() == 0


def test_enqueue_routes_quick_smell(validator):
    msg = build_block_validation_request(
        hash_id=_hash_bytes(2),
        prev_hash=_hash_bytes(200),
        validation_type=ValidationType.ValidationType.Quick_Smell,
    )
    validator.enqueue_request(msg)
    assert validator.quick_queue.qsize() == 0
    assert validator.quick_smell_queue.qsize() == 1


def test_enqueue_routes_full_sets_full_requested_and_mirrors_to_quick(validator):
    msg = build_block_validation_request(
        hash_id=_hash_bytes(3),
        prev_hash=_hash_bytes(300),
        validation_type=ValidationType.ValidationType.Full,
    )
    validator.enqueue_request(msg)
    assert validator.quick_queue.qsize() == 1  # mirrored
    assert validator.full_queue.qsize() == 1
    with validator.full_req_lock:
        assert _hash_bytes(3) in validator.full_requested


def test_enqueue_routes_model(validator):
    msg = build_model_validation_request(hash_id=_hash_bytes(4))
    validator.enqueue_request(msg)
    assert validator.model_queue.qsize() == 1


def test_quick_updates_status_event_dependency_and_sends(validator):
    import main as main
    # Enqueue quick
    h = _hash_bytes(5)
    prev = _hash_bytes(55)
    msg = build_block_validation_request(hash_id=h, prev_hash=prev, validation_type=ValidationType.ValidationType.Quick)
    validator.enqueue_request(msg)

    # Pop and validate
    _, _, req = validator.quick_queue.get_nowait()
    verifier = main.ProofVerifier()
    validator.validate_quick(req, verifier)

    # Status
    st = validator.validation_status[h]
    assert st["quick"] == ResponseValue.ResponseValue.Quick_OK
    # Dependency recorded
    with validator.dependency_lock:
        assert h in validator.block_dependencies[prev]
    # Response captured
    assert len(validator.sender.submitted) == 1


def test_quick_fail_propagates(validator):
    # Two blocks: B depends on A
    A = _hash_bytes(10)
    B = _hash_bytes(11)
    # Record dependency directly (simulate enqueue of B)
    with validator.dependency_lock:
        validator.block_dependencies[A].add(B)

    # Mark that Full was requested for B so that a response would be sent if needed
    with validator.full_req_lock:
        validator.full_requested.add(B)

    # Simulate quick fail for A and propagate
    validator.set_phase_result(A, "quick", ResponseValue.ResponseValue.Quick_Fail)
    validator.propagate_validation_failure(A)

    # B should be marked Full_Red and a response queued
    with validator.status_lock:
        assert validator.validation_status[B]["full"] == ResponseValue.ResponseValue.Full_Red
    assert any(True for _ in validator.sender.submitted)


def test_quick_smell_sets_both_phases_and_signal(validator):
    import services.verification_api.src.main as main
    h = _hash_bytes(20)
    prev = _hash_bytes(21)
    msg = build_block_validation_request(hash_id=h, prev_hash=prev, validation_type=ValidationType.ValidationType.Quick_Smell)
    validator.enqueue_request(msg)

    _, _, req = validator.quick_smell_queue.get_nowait()
    verifier = main.ProofVerifier()
    # Override smell to produce Quick_OK_Smell_Fail. **_kwargs accepts the
    # slice-11 target_override_hex forwarded by main.py:1030 without forcing
    # this test to track every production signature change.
    def smell_fail(_, **_kwargs):
        return ResponseValue.ResponseValue.Quick_OK_Smell_Fail
    verifier.quick_verify_smell_test = smell_fail

    validator.validate_quick_smell(req, verifier)

    st = validator.validation_status[h]
    assert st["smell"] == ResponseValue.ResponseValue.Quick_OK_Smell_Fail
    assert st["quick"] == ResponseValue.ResponseValue.Quick_OK
    assert len(validator.sender.submitted) == 1


def test_full_waits_on_quick_then_calls_verifier_and_final_sends(validator):
    import services.verification_api.src.main as main
    h = _hash_bytes(30)
    msg = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(31), validation_type=ValidationType.ValidationType.Full)
    validator.enqueue_request(msg)

    # Pretend quick is done successfully and signal event
    validator.set_phase_result(h, "quick", ResponseValue.ResponseValue.Quick_OK)
    validator._signal_validation_complete(h)

    # Pop from full queue and validate with stub verifier returning GREEN
    _, _, req = validator.full_queue.get_nowait()
    verifier = main.ProofVerifier()
    validator.validate_full(req, verifier)

    st = validator.validation_status[h]
    assert st["full"] == ResponseValue.ResponseValue.Full_Green
    # Full result should be sent
    assert len(validator.sender.submitted) == 1


def test_full_reenqueue_red_once_and_final_send(validator):
    import services.verification_api.src.main as main
    h = _hash_bytes(40)
    msg = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(41), validation_type=ValidationType.ValidationType.Full)
    validator.enqueue_request(msg)
    validator.set_phase_result(h, "quick", ResponseValue.ResponseValue.Quick_OK)
    validator._signal_validation_complete(h)

    _, _, req1 = validator.full_queue.get_nowait()

    # Verifier that returns RED then GREEN
    calls = {"n": 0}
    class _V:
        def full_verify(self, _, **_kwargs):
            calls["n"] += 1
            return "RED" if calls["n"] == 1 else "GREEN"

    validator.validate_full(req1, _V())
    # RED should re-enqueue without sending
    assert len(validator.sender.submitted) == 0
    assert validator.full_queue.qsize() == 1

    _, _, req2 = validator.full_queue.get_nowait()
    validator.validate_full(req2, _V())
    # Now GREEN → final send
    assert len(validator.sender.submitted) == 1


def test_full_reenqueue_amber_twice(validator):
    h = _hash_bytes(50)
    msg = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(51), validation_type=ValidationType.ValidationType.Full)
    validator.enqueue_request(msg)
    validator.set_phase_result(h, "quick", ResponseValue.ResponseValue.Quick_OK)
    validator._signal_validation_complete(h)

    _, _, req1 = validator.full_queue.get_nowait()

    seq = iter(["AMBER", "AMBER", "RED"])  # final becomes RED
    class _V:
        def full_verify(self, _, **_kwargs):
            return next(seq)

    # 1st: AMBER → re-enqueue
    validator.validate_full(req1, _V())
    assert validator.full_queue.qsize() == 1
    # 2nd: AMBER → re-enqueue
    _, _, req2 = validator.full_queue.get_nowait()
    validator.validate_full(req2, _V())
    assert validator.full_queue.qsize() == 1
    # 3rd: RED → final response
    _, _, req3 = validator.full_queue.get_nowait()
    validator.validate_full(req3, _V())
    assert len(validator.sender.submitted) == 1


def test_model_validation_ok_and_fail(validator, monkeypatch):
    # Patch model validator .validate method to match new signature:
    # validate(raw_message, claimed_difficulty=..., model_name=...) -> (status, report)
    # Return "pending_operator_review" to trigger the review pending path.
    validator.model_validator.validate = lambda _buf, **kw: ("pending_operator_review", {"test": True})
    h = _hash_bytes(60)
    msg = build_model_validation_request(hash_id=h)
    validator.enqueue_request(msg)
    _, _, req = validator.model_queue.get_nowait()
    validator.validate_model(req)
    assert len(validator.sender.submitted) == 1  # Model_Pending_Review

    # Now enqueue the same hash again — it's in pending_reviews,
    # so validate_model sends Model_Pending_Review again without re-auditing.
    validator.enqueue_request(msg)
    # The model queue should be empty (idempotent — already pending)
    assert validator.model_queue.qsize() == 0
    # But if we call validate_model directly with the same hash,
    # it should send Model_Pending_Review again from pending_reviews.
    validator.validate_model(req)
    assert len(validator.sender.submitted) == 2  # second Model_Pending_Review


def test_send_response_builds_flatbuffer(validator):
    from utils.proof import ValidationResponse
    h = _hash_bytes(70)
    validator.send_response(h, ResponseValue.ResponseValue.Quick_OK)
    assert len(validator.sender.submitted) == 1
    payload = validator.sender.submitted[0]
    resp = ValidationResponse.ValidationResponse.GetRootAs(payload, 0)
    got_hash = bytes(resp.HashIdentifierAsNumpy().tolist())
    assert got_hash == h
    assert resp.EnumResponse() == ResponseValue.ResponseValue.Quick_OK


def test_enqueue_error_path_missing_hash_id(validator):
    # Malformed buffer: empty bytes should trigger error handling.
    # New contract: log the parse failure but do NOT vote — local execution
    # errors must not be turned into validator votes.
    validator.enqueue_request(b"")
    assert validator.sender.submitted == []
    # Routing queues stay untouched for malformed input
    assert validator.quick_queue.qsize() == 0
    assert validator.full_queue.qsize() == 0
    assert validator.model_queue.qsize() == 0
import sys
if sys.version_info[0] < 3:
    import pytest as _pytest
    raise _pytest.SkipTest("Python >=3.6 required to import main module")
