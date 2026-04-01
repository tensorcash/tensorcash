# SPDX-License-Identifier: Apache-2.0
import time

import pytest

from utils.proof import (
    ValidationType,
    ValidationUnion,
    ResponseValue,
    ValidationResponse,
    ValidationRequest,
)
from helpers.fb_builders import (
    build_block_validation_request,
    build_model_validation_request,
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


@pytest.fixture()
def validator(monkeypatch):
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
    import main as main
    monkeypatch.setattr(main, "ZmqSendBroker", _FakeBroker)
    return main.AsyncValidator(pull_port=6020, push_host="127.0.0.1", push_port=7020)


def test_quick_exception_does_not_vote(validator, monkeypatch):
    """quick_verify raising must NOT produce a Quick_Fail_Smell_Fail vote.

    Contract: a local execution error in the verifier is logged but never
    turned into a validator vote against the miner.
    """
    import proof_verifier as pv
    def _boom(_):
        raise RuntimeError("boom")
    pv.ProofVerifier.quick_verify = _boom

    h = (b"\xaa" * 32)
    raw = build_block_validation_request(hash_id=h, prev_hash=b"\x00" * 32, validation_type=ValidationType.ValidationType.Quick)
    data = {
        'hash_id': h,
        'validation_type': ValidationType.ValidationType.Quick,
        'request': ValidationRequest.ValidationRequest.GetRootAs(raw, 0),
        'raw_message': raw,
        'timestamp': time.time(),
        'retry_count': 0,
    }
    validator.validate_quick(data, pv.ProofVerifier())

    assert validator.sender.submitted == []
    with validator.status_lock:
        assert h not in validator.validation_status


def test_full_exception_does_not_vote(validator, monkeypatch):
    """full_verify raising must NOT produce a Full_Red vote.

    Contract: when full_verify raises, the validator either re-enqueues for
    another attempt (within full_execution_retries) or, after exhaustion,
    silently clears the event. It never submits a Full_Red on the bus.
    Setting full_execution_retries=0 forces the exhaust path so the test
    is independent of env defaults.
    """
    import proof_verifier as pv
    def _boom(_):
        raise RuntimeError("boom")
    pv.ProofVerifier.full_verify = _boom

    validator.full_execution_retries = 0

    h = (b"\xbb" * 32)
    raw = build_block_validation_request(hash_id=h, prev_hash=b"\x00" * 32, validation_type=ValidationType.ValidationType.Full)
    data = {
        'hash_id': h,
        'validation_type': ValidationType.ValidationType.Full,
        'request': ValidationRequest.ValidationRequest.GetRootAs(raw, 0),
        'raw_message': raw,
        'timestamp': time.time(),
        'retry_count': 0,
        'execution_retry_count': 0,
    }
    validator.validate_full(data, pv.ProofVerifier())

    assert validator.sender.submitted == []
    with validator.status_lock:
        assert 'full' not in validator.validation_status.get(h, {})


def test_model_exception_does_not_vote(validator, monkeypatch):
    """model_validator raising must NOT produce a Model_Fail vote.

    Contract: a local model-audit execution error is logged but does not
    become a validator vote.
    """
    h = (b"\xcc" * 32)
    raw = build_model_validation_request(hash_id=h)
    data = {
        'hash_id': h,
        'validation_type': ValidationType.ValidationType.Model,
        'request': ValidationRequest.ValidationRequest.GetRootAs(raw, 0),
        'raw_message': raw,
        'timestamp': time.time(),
        'retry_count': 0,
    }
    validator.model_validator.validate = lambda _buf: (_ for _ in ()).throw(RuntimeError("boom"))
    validator.validate_model(data)

    assert validator.sender.submitted == []

