# SPDX-License-Identifier: Apache-2.0
import os
import sys
import pytest as _pytest
if sys.version_info[0] < 3:
    raise _pytest.SkipTest("Python >=3.6 required to import main module")
import socket
import time
import threading

import pytest
import zmq

from utils.proof import (
    ValidationType,
    ResponseValue,
    ValidationResponse,
)
from helpers.fb_builders import (
    build_block_validation_request,
    build_model_validation_request,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def zmq_ctx():
    ctx = zmq.Context()
    try:
        yield ctx
    finally:
        # Force fast teardown; avoid potential blocking on term in CI
        try:
            ctx.destroy(linger=0)
        except Exception:
            ctx.term()


def _start_workers(v, *, quick=False, quick_smell=False, full=False, model=False, recv=False):
    threads = []
    if recv:
        t = threading.Thread(target=v.receive_requests, daemon=True)
        t.start()
        threads.append(t)
    if quick:
        t = threading.Thread(target=v.process_quick_validations, daemon=True)
        t.start()
        threads.append(t)
    if quick_smell:
        t = threading.Thread(target=v.process_quick_smell_validations, daemon=True)
        t.start()
        threads.append(t)
    if full:
        t = threading.Thread(target=v.process_full_validations, daemon=True)
        t.start()
        threads.append(t)
    if model:
        t = threading.Thread(target=v.process_model_validations, daemon=True)
        t.start()
        threads.append(t)
    return threads


def _recv_one(sock, timeout_ms=5000):
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    socks = dict(poller.poll(timeout_ms))
    assert socks.get(sock) == zmq.POLLIN, "No response received in time"
    return sock.recv()


def test_e2e_quick_happy_path(zmq_ctx):
    # Arrange egress sink
    push_port = _free_port()
    sink = zmq_ctx.socket(zmq.PULL)
    sink.bind("tcp://*:%d" % push_port)

    # Create validator with real ZMQ
    pull_port = _free_port()
    from main import AsyncValidator
    v = AsyncValidator(pull_port=pull_port, push_host="127.0.0.1", push_port=push_port)
    v.sender.start()
    v.running = True
    threads = _start_workers(v, quick=True, recv=True)

    # Client PUSH socket
    client = zmq_ctx.socket(zmq.PUSH)
    client.connect("tcp://127.0.0.1:%d" % pull_port)

    h = (b"\x01" * 32)
    msg = build_block_validation_request(hash_id=h, prev_hash=b"\x00" * 32, validation_type=ValidationType.ValidationType.Quick)
    client.send(msg)

    # Expect Quick_OK
    payload = _recv_one(sink)
    resp = ValidationResponse.ValidationResponse.GetRootAs(payload, 0)
    assert bytes(resp.HashIdentifierAsNumpy().tolist()) == h
    assert resp.EnumResponse() == ResponseValue.ResponseValue.Quick_OK

    # Teardown
    v.shutdown()
    for t in threads:
        t.join(timeout=0.5)
    sink.close(0)
    client.close(0)


def test_e2e_full_after_quick_ok(zmq_ctx):
    push_port = _free_port()
    sink = zmq_ctx.socket(zmq.PULL)
    sink.bind("tcp://*:%d" % push_port)

    pull_port = _free_port()
    from main import AsyncValidator
    v = AsyncValidator(pull_port=pull_port, push_host="127.0.0.1", push_port=push_port)
    v.sender.start()
    v.running = True
    threads = _start_workers(v, quick=True, full=True, recv=True)

    client = zmq_ctx.socket(zmq.PUSH)
    client.connect("tcp://127.0.0.1:%d" % pull_port)

    h = (b"\x02" * 32)
    # First quick
    client.send(build_block_validation_request(hash_id=h, prev_hash=b"\x00" * 32, validation_type=ValidationType.ValidationType.Quick))
    payload1 = _recv_one(sink)
    r1 = ValidationResponse.ValidationResponse.GetRootAs(payload1, 0)
    assert r1.EnumResponse() in (ResponseValue.ResponseValue.Quick_OK, ResponseValue.ResponseValue.Quick_OK_Smell_OK)

    # Then full
    client.send(build_block_validation_request(hash_id=h, prev_hash=b"\x00" * 32, validation_type=ValidationType.ValidationType.Full))
    payload2 = _recv_one(sink)
    r2 = ValidationResponse.ValidationResponse.GetRootAs(payload2, 0)
    # Could receive quick mirror first; keep reading until Full_*
    if r2.EnumResponse() in (ResponseValue.ResponseValue.Quick_OK, ResponseValue.ResponseValue.Quick_OK_Smell_OK):
        payload2 = _recv_one(sink)
        r2 = ValidationResponse.ValidationResponse.GetRootAs(payload2, 0)
    assert r2.EnumResponse() in (ResponseValue.ResponseValue.Full_Green, ResponseValue.ResponseValue.Full_Amber, ResponseValue.ResponseValue.Full_Red)

    v.shutdown()
    for t in threads:
        t.join(timeout=0.5)
    sink.close(0)
    client.close(0)


def test_e2e_full_short_circuit_on_quick_fail(zmq_ctx, monkeypatch):
    # Force quick to fail
    import proof
    import proof.ResponseValue as RV
    import proof_verifier as pv
    def _qf(_):
        return RV.ResponseValue.Quick_Fail
    pv.ProofVerifier.quick_verify = _qf

    push_port = _free_port()
    sink = zmq_ctx.socket(zmq.PULL)
    sink.bind("tcp://*:%d" % push_port)

    pull_port = _free_port()
    from main import AsyncValidator
    v = AsyncValidator(pull_port=pull_port, push_host="127.0.0.1", push_port=push_port)
    v.sender.start()
    v.running = True
    threads = _start_workers(v, quick=True, full=True, recv=True)

    client = zmq_ctx.socket(zmq.PUSH)
    client.connect("tcp://127.0.0.1:%d" % pull_port)

    h = (b"\x03" * 32)
    # Request Full (will enqueue quick first), quick will fail, full should short-circuit to RED
    client.send(build_block_validation_request(hash_id=h, prev_hash=b"\x00" * 32, validation_type=ValidationType.ValidationType.Full))

    # Expect Quick_Fail first, then Full_Red
    p1 = _recv_one(sink)
    r1 = ValidationResponse.ValidationResponse.GetRootAs(p1, 0)
    # The first could be quick or full depending on scheduling, so read until Full_*
    if r1.EnumResponse() not in (ResponseValue.ResponseValue.Full_Green, ResponseValue.ResponseValue.Full_Amber, ResponseValue.ResponseValue.Full_Red):
        p2 = _recv_one(sink)
        r2 = ValidationResponse.ValidationResponse.GetRootAs(p2, 0)
        r_final = r2
    else:
        r_final = r1
    assert r_final.EnumResponse() == ResponseValue.ResponseValue.Full_Red

    v.shutdown()
    for t in threads:
        t.join(timeout=0.5)
    sink.close(0)
    client.close(0)


def test_e2e_model_validation(zmq_ctx):
    push_port = _free_port()
    sink = zmq_ctx.socket(zmq.PULL)
    sink.bind("tcp://*:%d" % push_port)

    pull_port = _free_port()
    from main import AsyncValidator
    v = AsyncValidator(pull_port=pull_port, push_host="127.0.0.1", push_port=push_port)
    # Stub model validate — signature is (raw, claimed_difficulty=, model_name=) -> (status, report)
    v.model_validator.validate = lambda _buf, **kw: ("pending_operator_review", {"test": True})
    v.sender.start()
    v.running = True
    threads = _start_workers(v, model=True, recv=True)

    client = zmq_ctx.socket(zmq.PUSH)
    client.connect("tcp://127.0.0.1:%d" % pull_port)

    h = (b"\x04" * 32)
    client.send(build_model_validation_request(hash_id=h))
    payload = _recv_one(sink)
    r = ValidationResponse.ValidationResponse.GetRootAs(payload, 0)
    assert r.EnumResponse() == ResponseValue.ResponseValue.Model_Pending_Review

    v.shutdown()
    for t in threads:
        t.join(timeout=0.5)
    sink.close(0)
    client.close(0)
