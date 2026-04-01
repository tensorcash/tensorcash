# SPDX-License-Identifier: Apache-2.0
import threading
import time

import pytest

from utils.proof import ValidationType, ResponseValue
from helpers.fb_builders import build_block_validation_request, build_model_validation_request


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


class CountingVerifier:
    quick_calls = 0
    smell_calls = 0
    full_calls = 0

    # **_kwargs swallows any future production kwargs (currently
    # target_override_hex from slice-11 share-mode forwarding) so the
    # mock stays forward-compatible without breaking on each
    # signature additions in main.py.
    def quick_verify(self, _buf, **_kwargs):
        type(self).quick_calls += 1
        time.sleep(0.1)
        return ResponseValue.ResponseValue.Quick_OK

    def quick_verify_smell_test(self, _buf, **_kwargs):
        type(self).smell_calls += 1
        time.sleep(0.1)
        return ResponseValue.ResponseValue.Quick_OK_Smell_OK

    def full_verify(self, _buf, **_kwargs):
        type(self).full_calls += 1
        time.sleep(0.2)
        return "GREEN"


def _hash_bytes(seed: int) -> bytes:
    return (seed.to_bytes(4, "big") * 8)[:32]


@pytest.fixture()
def validator(monkeypatch):
    # Pre-seed a lightweight zmq stub before importing main
    import types, sys
    if 'zmq' not in sys.modules:
        zmq_stub = types.ModuleType('zmq')
        zmq_stub.PULL = 0
        zmq_stub.PUSH = 1
        def _ctx():
            return FakeContext()
        zmq_stub.Context = _ctx
        def _linger(*args, **kwargs):
            return None
        zmq_stub.LINGER = 0
        zmq_stub.EAGAIN = 0
        sys.modules['zmq'] = zmq_stub

    import services.verification_api.src.main as main
    # Patch sender broker directly on the imported module
    monkeypatch.setattr(main, "ZmqSendBroker", FakeBroker)
    # Replace ProofVerifier with our counting+sleeping stub
    monkeypatch.setattr(main, "ProofVerifier", CountingVerifier)

    v = main.AsyncValidator(pull_port=6200, push_host="127.0.0.1", push_port=7200)
    return v


def test_quick_duplicate_is_idempotent(validator):
    # Start only the quick worker
    t = threading.Thread(target=validator.process_quick_validations, daemon=True)
    t.start()

    h = _hash_bytes(101)
    msg = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(1), validation_type=ValidationType.ValidationType.Quick)

    # Enqueue the same quick request twice rapidly
    validator.enqueue_request(msg)
    validator.enqueue_request(msg)

    # Allow worker to process
    time.sleep(0.35)
    validator.running = False
    t.join(timeout=1.0)

    # Only one quick verification should have run
    assert CountingVerifier.quick_calls == 1
    # And only one response should have been emitted
    assert len(validator.sender.submitted) == 1


def test_full_duplicate_inflight_is_idempotent_and_cached_after_complete(validator):
    # Prepare gating: quick already done for this hash
    h = _hash_bytes(202)
    validator.set_phase_result(h, 'quick', ResponseValue.ResponseValue.Quick_OK)
    validator._signal_validation_complete(h)

    # Start full worker
    t = threading.Thread(target=validator.process_full_validations, daemon=True)
    t.start()

    msg = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(2), validation_type=ValidationType.ValidationType.Full)

    # Enqueue full twice while the first will still be processing
    validator.enqueue_request(msg)
    validator.enqueue_request(msg)

    # Allow processing to finish
    time.sleep(0.5)
    validator.running = False
    t.join(timeout=1.0)

    # Full verifier should have run only once (duplicate ignored)
    assert CountingVerifier.full_calls == 1
    # One response from the completed full verification
    assert len(validator.sender.submitted) == 1

    # Now enqueue the same Full again after completion: should return cached result immediately
    validator.running = True  # temporarily allow enqueue path (no worker needed)
    validator.enqueue_request(msg)
    # Cached path sends immediately without recompute
    assert len(validator.sender.submitted) == 2
    validator.running = False


def test_model_duplicate_is_idempotent_and_cached(validator, monkeypatch):
    # Start model worker
    t = threading.Thread(target=validator.process_model_validations, daemon=True)
    t.start()

    h = _hash_bytes(303)
    msg = build_model_validation_request(hash_id=h)

    # Ensure model validator has a .validate method matching the new signature:
    # validate(raw_message, claimed_difficulty=..., model_name=...) -> (status, report)
    validator.model_validator.validate = lambda _buf, **kw: ("pending_operator_review", {"test": True})

    # Enqueue twice; only one should be processed (idempotent dedup)
    validator.enqueue_request(msg)
    validator.enqueue_request(msg)

    time.sleep(0.25)
    validator.running = False
    t.join(timeout=1.0)

    # One model response sent (Model_Pending_Review) — dedup prevented second enqueue
    assert len(validator.sender.submitted) == 1
    # Pending review stored for this hash
    assert h in validator.pending_reviews
    validator.running = False
