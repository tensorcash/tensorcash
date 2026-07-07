"""Stage-4 sampler tests (TIP-0003): RingBuffers admission
state and batch_sample_tokens appending the stored 32 nonce bytes on every v3
step, byte-identical to the pow_v3 reference; no-admission rows keep the
legacy v2 message shape."""

import os
import sys

import pytest

_POW_UTILS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _POW_UTILS_DIR)
# zmq_pow_writer (imported via the csh_real fixture) needs the real generated
# FlatBuffers package, not the local `proof/` mock — same eviction dance as
# test_pow_v3_carrier.py.
sys.path.insert(0, os.path.join(os.path.dirname(_POW_UTILS_DIR), "fb-schemas"))
import importlib

_p = sys.modules.get("proof")
if _p is not None and not hasattr(getattr(_p, "Proof", None), "Proof"):
    for _m in [m for m in list(sys.modules)
               if m == "proof" or m.startswith("proof.")]:
        sys.modules.pop(_m, None)
    for _m in ("pow_utils", "zmq_pow_writer"):
        sys.modules.pop(_m, None)
    importlib.invalidate_caches()

torch = pytest.importorskip("torch")

import pow_v3
from pow_utils import POW_WINDOW_SIZE, PowHasher, RingBuffers

HEADER = bytes(range(76))
VDF = bytes(range(100, 132))
NONCE = bytes(range(32))
TICK = 42
PRECISION = "fp16"


def _snapshot():
    return {
        "tick": TICK,
        "request_id": 7,
        "header_prefix": HEADER.hex(),
        "vdf": VDF.hex(),
        "block_hash": "bb" * 32,
        "target": "ff" * 32,
    }


@pytest.fixture
def rig():
    rb = RingBuffers(window_size=POW_WINDOW_SIZE, max_rows=4, device="cpu")
    for row in range(3):
        rb.write_pow_params(row, _snapshot())
    hasher = PowHasher(device="cpu")
    return rb, hasher


def _sample(hasher, rb, rows, contexts, steps):
    B = len(rows)
    ctx = torch.zeros(B, POW_WINDOW_SIZE, dtype=torch.int64)
    for i, c in enumerate(contexts):
        ctx[i, -len(c):] = torch.tensor(c, dtype=torch.int64)
    cdfs = torch.linspace(0, 1, 101).unsqueeze(0).expand(B, -1).contiguous()
    return hasher.batch_sample_tokens(
        ctx, torch.tensor(steps, dtype=torch.int32), cdfs, PRECISION,
        ring_buffers=rb, rows_tensor=torch.tensor(rows), rows_host=rows)


def _ref_digest(ctx, step, nonce):
    msg = pow_v3.build_step_message(HEADER, VDF, TICK, step, ctx, PRECISION,
                                    admission_nonce=nonce)
    return pow_v3.step_u_from_message(msg)[1]


class TestAdmissionState:
    def test_write_and_clear(self, rig):
        rb, _ = rig
        rb.write_admission_nonce(1, NONCE)
        assert bool(rb.pow_admission_valid[1])
        assert rb.pow_admission_valid_host[1]
        assert bytes(rb.pow_admission_nonce[1].tolist()) == NONCE
        rb.write_admission_nonce(1, None)
        assert not bool(rb.pow_admission_valid[1])
        assert not rb.pow_admission_valid_host[1]
        rb.write_admission_nonce(1, NONCE)
        rb.clear_row(1)
        assert not bool(rb.pow_admission_valid[1])
        assert not rb.pow_admission_valid_host[1]
        assert bytes(rb.pow_admission_nonce[1].tolist()) == bytes(32)

    def test_rejects_wrong_length(self, rig):
        rb, _ = rig
        with pytest.raises(ValueError):
            rb.write_admission_nonce(0, b"\x01" * 31)

    def test_clear_rows_batch_clears_admission(self, rig):
        # clear_rows (batch variant used on resets/eviction) must clear v3
        # admission state too — a stale nonce leaking into a reallocated row
        # would corrupt every u of its first window. It must ALSO clear the
        # share target and validity flag (matches clear_row): a recycled row
        # keeping a stale share_target would emit shares against its prior
        # occupant's target, and staying pow_valid=True would surface a row
        # with zeroed params as live.
        rb, _ = rig
        for row in (0, 2):
            rb.write_admission_nonce(row, NONCE)
            rb.pow_share_target[row] = 0x5A
            rb.pow_valid[row] = True
        rb.clear_rows([0, 2])
        for row in (0, 2):
            assert not bool(rb.pow_admission_valid[row])
            assert not rb.pow_admission_valid_host[row]
            assert bytes(rb.pow_admission_nonce[row].tolist()) == bytes(32)
            assert bytes(rb.pow_share_target[row].tolist()) == bytes(32)
            assert not bool(rb.pow_valid[row])


class TestBatchSampleNonce:
    def test_fast_path_all_rows_with_nonce(self, rig):
        rb, hasher = rig
        contexts = [[1, 2, 3], [4, 5]]
        for row in (0, 1):
            rb.write_admission_nonce(row, NONCE)
        _, _, digests = _sample(hasher, rb, [0, 1], contexts, [9, 11])
        for i, (ctx, step) in enumerate(zip(contexts, (9, 11))):
            assert bytes(digests[i].tolist()) == _ref_digest(ctx, step, NONCE)

    def test_fast_path_no_nonce_keeps_legacy_shape(self, rig):
        rb, hasher = rig
        contexts = [[1, 2, 3], [4, 5]]
        _, _, digests = _sample(hasher, rb, [0, 1], contexts, [9, 11])
        for i, (ctx, step) in enumerate(zip(contexts, (9, 11))):
            assert bytes(digests[i].tolist()) == _ref_digest(ctx, step, None)

    def test_mixed_batch_per_row_path(self, rig):
        rb, hasher = rig
        contexts = [[1, 2, 3], [4, 5], [6]]
        rb.write_admission_nonce(1, NONCE)   # only the middle row admits
        _, _, digests = _sample(hasher, rb, [0, 1, 2], contexts, [9, 11, 200])
        assert bytes(digests[0].tolist()) == _ref_digest(contexts[0], 9, None)
        assert bytes(digests[1].tolist()) == _ref_digest(contexts[1], 11, NONCE)
        assert bytes(digests[2].tolist()) == _ref_digest(contexts[2], 200, None)

    def test_nonce_changes_every_u(self, rig):
        rb, hasher = rig
        contexts = [[1, 2, 3]]
        _, us_plain, _ = _sample(hasher, rb, [0], contexts, [9])
        rb.write_admission_nonce(0, NONCE)
        _, us_nonce, _ = _sample(hasher, rb, [0], contexts, [9])
        assert us_plain[0].item() != us_nonce[0].item()


@pytest.fixture()
def csh_real(monkeypatch):
    """Import common_sampler_helper with the REAL pow_utils bound as
    vllm.sampling.pow_utils (other test files stub it with MagicMock; this
    fixture restores sys.modules afterwards so the two styles don't bleed
    into each other)."""
    import types

    import pow_utils as real_pow_utils
    import uint256_arithmetics as real_uint256
    import zmq_pow_writer as real_zmq_pow_writer

    monkeypatch.setitem(sys.modules, "vllm", types.ModuleType("vllm"))
    monkeypatch.setitem(sys.modules, "vllm.sampling",
                        types.ModuleType("vllm.sampling"))
    monkeypatch.setitem(sys.modules, "vllm.sampling.pow_utils", real_pow_utils)
    monkeypatch.setitem(sys.modules, "vllm.sampling.zmq_pow_writer",
                        real_zmq_pow_writer)
    monkeypatch.setitem(sys.modules, "vllm.sampling.uint256_arithmetics",
                        real_uint256)
    monkeypatch.delitem(sys.modules, "common_sampler_helper", raising=False)
    import common_sampler_helper as csh
    yield csh
    sys.modules.pop("common_sampler_helper", None)


class TestFinalDigestNonceWiring:
    """TIP-0003 regression: the target-critical FINAL digest in
    CommonSamplerHelper includes the row's admission nonce, and the selected
    nonce reaches both proof writers (Python write_proof kwarg, C++
    pow_hasher_data) without re-grinding."""

    WINDOW = 8
    SEQ = "seq_v3"
    ROW = 0

    def _owner(self):
        from collections import deque
        from unittest.mock import Mock

        from pow_utils import PowHasher

        owner = Mock()
        owner.window_size = self.WINDOW
        owner.device = "cpu"
        owner.logger = Mock()
        owner.ring_buffers = RingBuffers(window_size=self.WINDOW, max_rows=2,
                                         device="cpu")
        owner.ring_buffers.steps[self.ROW] = self.WINDOW  # one full window
        owner.ring_buffers.chosen_tokens[:, self.ROW] = torch.arange(
            1, self.WINDOW + 1, dtype=torch.int64)
        owner.pow_hasher = PowHasher(device="cpu")
        owner.proof_writer = Mock()
        owner.proof_writer.compute_precision = PRECISION
        owner.proof_writer.write_proof = Mock(return_value=(b"blob", {"d": 1}))
        owner.submitter = Mock()
        owner.seq_caches = {self.SEQ: {"archive_list": list(range(20)),
                                       "pad_mask_list": [False] * 20}}
        owner.seq_params = {self.SEQ: {
            "pow_snapshot": {
                "tick": TICK,
                "header_prefix": HEADER.hex(),
                "vdf": VDF.hex(),
                "block_hash": "bb" * 32,
                "target": "ff" * 32,      # everything is a solution
                "ipfs_cid": "cid",
                "request_id": 1,
                "difficulty": 1_000_000,
            },
            "completion_id": "cmpl-v3",
            "temperature": 1.0, "top_p": 1.0, "top_k": 50,
            "repetition_penalty": 1.0,
        }}
        return owner

    def _helper(self, owner, csh):
        return csh.CommonSamplerHelper(owner, proxy_audit_enabled=False)

    def _expected_digest(self, nonce):
        window_tokens = list(range(1, self.WINDOW + 1))
        msg = pow_v3.build_step_message(
            HEADER, VDF, TICK, 0, window_tokens, PRECISION,
            admission_nonce=nonce, window_size=self.WINDOW)
        return pow_v3.step_u_from_message(msg)[1]

    def test_python_path_digest_and_writer_kwarg(self, csh_real):
        owner = self._owner()
        helper = self._helper(owner, csh_real)
        owner.ring_buffers.write_admission_nonce(self.ROW, NONCE)
        helper._process_solution_python(self.SEQ, self.ROW)

        call = owner.proof_writer.write_proof.call_args
        assert call.kwargs["admission_nonce"] == NONCE
        digest_arg = call.args[3]
        assert bytes(digest_arg[0].tolist()) == self._expected_digest(NONCE)
        # nonce changes the final digest vs the legacy shape
        assert self._expected_digest(NONCE) != self._expected_digest(None)

    def test_python_path_without_nonce_keeps_legacy_digest(self, csh_real):
        owner = self._owner()
        helper = self._helper(owner, csh_real)
        helper._process_solution_python(self.SEQ, self.ROW)
        call = owner.proof_writer.write_proof.call_args
        assert call.kwargs["admission_nonce"] is None
        digest_arg = call.args[3]
        assert bytes(digest_arg[0].tolist()) == self._expected_digest(None)

    def test_cpp_path_pow_hasher_data_carries_nonce(self, csh_real):
        from unittest.mock import Mock

        owner = self._owner()
        helper = self._helper(owner, csh_real)
        helper.proof_processor = Mock()
        helper.proof_processor.process_proof = Mock(return_value={"queued": True})
        owner.ring_buffers.write_admission_nonce(self.ROW, NONCE)
        helper._process_solution_cpp(self.SEQ, self.ROW)

        kwargs = helper.proof_processor.process_proof.call_args.kwargs
        assert kwargs["pow_hasher_data"]["admission_nonce"] == NONCE
        assert bytes(kwargs["digest"].tobytes()) == self._expected_digest(NONCE)

    def test_cpp_path_without_nonce_omits_key(self, csh_real):
        from unittest.mock import Mock

        owner = self._owner()
        helper = self._helper(owner, csh_real)
        helper.proof_processor = Mock()
        helper.proof_processor.process_proof = Mock(return_value={"queued": True})
        helper._process_solution_cpp(self.SEQ, self.ROW)

        kwargs = helper.proof_processor.process_proof.call_args.kwargs
        assert "admission_nonce" not in kwargs["pow_hasher_data"]
        assert bytes(kwargs["digest"].tobytes()) == self._expected_digest(None)
