#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
C++ ProofProcessor audit-emit coverage.

Exercises the real compiled ``proof_processor`` extension (the live path
in production, ``POW_PROCESSOR_MODE=cpp``) to prove that an
``audit_emit=True`` proof:

  1. is actually emitted in broker mode (the old behaviour silently
     dropped it — the 27B audit path regression the reviewer caught),
  2. carries ``proof_purpose=audit`` in ``Proof.extra_flags`` so the
     ProofCollector classifies it as audit BEFORE any mining filter, and
  3. is marked ``is_solution=False`` on the wire even if a block-tier
     digest is passed, so nothing downstream can mistake it for a hit.

Skipped automatically when the C++ extension or the generated ``proof``
FlatBuffers package is not importable (e.g. on a dev host without the
built ``.so``); runs in the Docker/CI image where both exist.
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

proof_processor = pytest.importorskip("proof_processor")
zmq = pytest.importorskip("zmq")
# Generated FlatBuffers package (built via flatc from proof.fbs).
MiningResponse = pytest.importorskip("proof.MiningResponse")


WINDOW_SIZE = 256
COLLECTOR_PORT = 7002  # broker-mode default primary destination


def _make_inputs(is_solution: bool):
    """Minimal but well-formed process_proof inputs (mirrors the shapes
    in test_proof_processor_equivalence.create_mock_data)."""
    topk_shape = (WINDOW_SIZE, 50)
    window_data = {
        "tokens": np.arange(WINDOW_SIZE, dtype=np.int32),
        "probs": np.ones(WINDOW_SIZE, dtype=np.float32) * 0.5,
        "topk_logits": np.zeros(topk_shape, dtype=np.float32),
        "topk_indices": np.zeros(topk_shape, dtype=np.int32),
        "attention_mask": np.ones(WINDOW_SIZE, dtype=bool),
        "sampling_u": np.zeros(WINDOW_SIZE, dtype=np.float32),
        "softmax_normalizers": np.ones(WINDOW_SIZE, dtype=np.float32),
        "logsumexp_stats": np.zeros((WINDOW_SIZE, 2), dtype=np.float32),
    }
    cache = {"archive_list": [], "pad_mask_list": []}
    pow_hasher = {
        "tick": 42,
        "target": bytes([0xFF] * 32),
        "vdf": bytes([0xAA] * 32),
        "block_hash": bytes([0xBB] * 32),
        "header_prefix": bytes([0xCC] * 32),
        "ipfs_cid": "QmTest123",
        "request_id": 99999,
        "difficulty": 1000000,
        "window_size": WINDOW_SIZE,
    }
    seq_params = {
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 40,
        "repetition_penalty": 1.1,
    }
    return dict(
        seq_id=12345,
        step_num=WINDOW_SIZE,
        cache_data=cache,
        window_data=window_data,
        digest=np.frombuffer(bytes([0x00] * 32), dtype=np.uint8),
        is_solution=is_solution,
        pow_hasher_data=pow_hasher,
        seq_params=seq_params,
        completion_id="cmpl-audit-test",
    )


@pytest.fixture
def broker_collector(monkeypatch):
    """Bind a PULL socket on the broker-mode primary destination so the
    ProofProcessor's writer (PUSH connect) delivers here, then yield a
    receiver. Env is set BEFORE the processor/writer is constructed."""
    monkeypatch.setenv("POW_EGRESS_MODE", "broker")
    monkeypatch.setenv("POW_PROXY_ENABLE", "false")
    monkeypatch.setenv("ZMQ_PUSH_HOST", "127.0.0.1")
    monkeypatch.setenv("ZMQ_PUSH_PORT", str(COLLECTOR_PORT))

    ctx = zmq.Context.instance()
    pull = ctx.socket(zmq.PULL)
    pull.bind(f"tcp://127.0.0.1:{COLLECTOR_PORT}")
    pull.setsockopt(zmq.RCVTIMEO, 5000)
    try:
        yield pull
    finally:
        pull.close(linger=0)


def _recv_proof(pull):
    buf = pull.recv()
    mr = MiningResponse.MiningResponse.GetRootAs(buf, 0)
    proof = mr.PowBlob()
    assert proof is not None, "MiningResponse carried no PowBlob"
    extra = proof.ExtraFlags()
    if isinstance(extra, bytes):
        extra = extra.decode("utf-8")
    return mr, proof, extra


def test_cpp_audit_emit_stamps_purpose_and_not_solution(broker_collector):
    proc = proof_processor.ProofProcessor()
    # Pass is_solution=True on purpose — audit_emit must force the wire
    # bit to False and route to the audit path regardless.
    proc.process_proof(**_make_inputs(is_solution=True), audit_emit=True)

    _mr, proof, extra = _recv_proof(broker_collector)
    assert extra, "extra_flags empty; proof_purpose marker missing"
    parsed = json.loads(extra)
    assert parsed.get("proof_purpose") == "audit"
    assert proof.IsSolution() is False, "audit proof must not be a solution on the wire"


def test_cpp_non_audit_does_not_stamp_audit_purpose(broker_collector):
    """A normal (non-audit) emission must NOT carry proof_purpose=audit —
    guards against the marker leaking onto mining frames."""
    proc = proof_processor.ProofProcessor()
    # is_solution=True → mining solution path, audit_emit default False.
    proc.process_proof(**_make_inputs(is_solution=True))

    _mr, _proof, extra = _recv_proof(broker_collector)
    if extra:
        try:
            parsed = json.loads(extra)
            assert parsed.get("proof_purpose") != "audit"
        except json.JSONDecodeError:
            pass  # non-JSON extra_flags is fine; just must not be audit JSON


def test_cpp_non_solution_non_share_does_not_emit_mining_frame(broker_collector):
    """A non-solution proof is not automatically a share.

    The sampler must explicitly pass is_share=True after checking
    header_hash <= adjusted_share_target. Otherwise the C++ processor
    would over-emit broker shares and the broker would reject them as
    above_share_target.
    """
    proc = proof_processor.ProofProcessor()
    result = proc.process_proof(
        **_make_inputs(is_solution=False),
        is_share=False,
    )

    assert result["queued"] is False
    with pytest.raises(zmq.error.Again):
        broker_collector.recv()


def test_cpp_explicit_share_emits_non_solution_frame(broker_collector):
    """When the caller has positively classified a sub-block share,
    C++ submits it to the broker path with Proof.is_solution=false."""
    proc = proof_processor.ProofProcessor()
    result = proc.process_proof(
        **_make_inputs(is_solution=False),
        is_share=True,
    )

    assert result["queued"] is True
    _mr, proof, _extra = _recv_proof(broker_collector)
    assert proof.IsSolution() is False
