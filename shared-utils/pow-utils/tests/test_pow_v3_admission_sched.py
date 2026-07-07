"""Stage-4 admission-grind scheduling tests (TIP-0003):
CommonSamplerHelper.ensure_admission_for_rows/-window grinds via the injected
native-style grind function at every window boundary with byte-exact inputs
(msg_w, prompt commitment, target, model id), stores/clears the nonce in
RingBuffers before the window's first sampled token, and never grinds when
the mode is off or the proof version is v2."""

import os
import sys
import time
from unittest.mock import Mock

import pytest

_POW_UTILS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _POW_UTILS_DIR)
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

try:
    import argon2  # noqa: F401
    HAVE_ARGON2 = True
except ImportError:
    HAVE_ARGON2 = False

HEADER = bytes(range(76))
VDF = bytes(range(100, 132))
NONCE = bytes(range(32))
TICK = 42
PRECISION = "fp16"
MODEL_ID = "org/model@abcdef012345"
DIFFICULTY = 1_000_000            # expected_tries = 60 at chain defaults
SEQ = "seq_v3"
ROW = 0


@pytest.fixture()
def csh_real(monkeypatch):
    """Import common_sampler_helper with the REAL pow_utils/pow_v3 bound as
    vllm.sampling.* (other test files stub them with MagicMock)."""
    import types

    import pow_utils as real_pow_utils
    import uint256_arithmetics as real_uint256
    import zmq_pow_writer as real_zmq_pow_writer

    monkeypatch.setitem(sys.modules, "vllm", types.ModuleType("vllm"))
    monkeypatch.setitem(sys.modules, "vllm.sampling",
                        types.ModuleType("vllm.sampling"))
    monkeypatch.setitem(sys.modules, "vllm.sampling.pow_utils", real_pow_utils)
    monkeypatch.setitem(sys.modules, "vllm.sampling.pow_v3", pow_v3)
    monkeypatch.setitem(sys.modules, "vllm.sampling.zmq_pow_writer",
                        real_zmq_pow_writer)
    monkeypatch.setitem(sys.modules, "vllm.sampling.uint256_arithmetics",
                        real_uint256)
    monkeypatch.delitem(sys.modules, "common_sampler_helper", raising=False)
    import common_sampler_helper as csh
    yield csh
    sys.modules.pop("common_sampler_helper", None)


def _owner(prompt=(1, 2, 3)):
    owner = Mock()
    owner.window_size = POW_WINDOW_SIZE
    owner.device = "cpu"
    owner.logger = Mock()
    owner.ring_buffers = RingBuffers(window_size=POW_WINDOW_SIZE, max_rows=2,
                                     device="cpu")
    owner.pow_hasher = PowHasher(device="cpu")
    owner.proof_writer = Mock()
    owner.proof_writer.compute_precision = PRECISION
    owner.proof_writer.proof_version = 3
    owner.proof_writer.model_identifier = MODEL_ID
    owner.seq_caches = {SEQ: {"archive_list": list(prompt),
                              "pad_mask_list": [False] * len(prompt)}}
    owner.seq_params = {SEQ: {"pow_snapshot": {
        "tick": TICK,
        "header_prefix": HEADER.hex(),
        "vdf": VDF.hex(),
        "block_hash": "bb" * 32,
        "target": "ff" * 32,
        "ipfs_cid": "cid",
        "request_id": 1,
        "difficulty": DIFFICULTY,
    }}}
    return owner


def _helper(csh, owner, mode="always", grind_fn=None):
    os.environ["POW_V3_ADMISSION_MODE"] = mode
    try:
        h = csh.CommonSamplerHelper(owner, proxy_audit_enabled=False)
    finally:
        os.environ.pop("POW_V3_ADMISSION_MODE", None)
    if grind_fn is not None:
        h._admission_grind_fn = grind_fn
    return h


def _ctx_rows(*contexts):
    out = torch.zeros(len(contexts), POW_WINDOW_SIZE, dtype=torch.int64)
    for i, c in enumerate(contexts):
        if c:
            out[i, -len(c):] = torch.tensor(c, dtype=torch.int64)
    return out


class _GrindSpy:
    """Grind stand-in with the native signature; records call args."""

    def __init__(self, result=NONCE):
        self.result = result
        self.calls = []

    def __call__(self, msg_w, model_identifier, target_le, max_tries,
                 prompt_commitment):
        self.calls.append(dict(msg_w=bytes(msg_w),
                               model_identifier=model_identifier,
                               target_le=bytes(target_le),
                               max_tries=int(max_tries),
                               prompt_commitment=bytes(prompt_commitment)))
        return self.result


class TestBoundaryScheduling:
    def test_grinds_exact_inputs_at_boundary(self, csh_real):
        owner = _owner()
        spy = _GrindSpy()
        h = _helper(csh_real, owner, grind_fn=spy)
        ctx = [1, 2, 3]
        h.ensure_admission_for_rows([SEQ], [ROW],
                                    torch.tensor([0], dtype=torch.int32),
                                    _ctx_rows(ctx), PRECISION)
        assert len(spy.calls) == 1
        call = spy.calls[0]
        # msg_w: byte-exact window-first-step preimage, no nonce. The context
        # row is the padded rolling window; padding zeros are preserved.
        assert call["msg_w"] == pow_v3.build_step_message(
            HEADER, VDF, TICK, 0, [0] * (POW_WINDOW_SIZE - 3) + ctx, PRECISION)
        assert call["model_identifier"] == MODEL_ID
        assert call["target_le"] == pow_v3.admission_target(
            DIFFICULTY).to_bytes(32, "little")
        assert call["max_tries"] == (
            pow_v3.admission_expected_tries(DIFFICULTY)
            * h.admission_max_tries_factor)
        assert call["prompt_commitment"] == pow_v3.prompt_commitment(
            [1, 2, 3], [False] * 3)
        # nonce stored BEFORE the window's first sampled token
        assert owner.ring_buffers.pow_admission_valid_host[ROW]
        assert bytes(owner.ring_buffers.pow_admission_nonce[ROW].tolist()) == NONCE

    def test_parallel_grind_all_boundary_rows(self, csh_real):
        """Two rows at a boundary both grind + store via the concurrent path
        (thread pool); each row is prepared from its OWN cache (distinct
        commitments) and the result is order-independent."""
        owner = _owner()
        SEQ2, ROW2 = "seq_v3_b", 1
        owner.seq_caches[SEQ2] = {"archive_list": [7, 8, 9, 10],
                                  "pad_mask_list": [False] * 4}
        owner.seq_params[SEQ2] = owner.seq_params[SEQ]
        spy = _GrindSpy()
        h = _helper(csh_real, owner, grind_fn=spy)
        h.ensure_admission_for_rows(
            [SEQ, SEQ2], [ROW, ROW2], torch.tensor([0, 0], dtype=torch.int32),
            _ctx_rows([1, 2, 3], [7, 8, 9, 10]), PRECISION)
        assert len(spy.calls) == 2
        # distinct prompt commitments prove each row used its own cache (the
        # pool may run them in either order, so compare as a set)
        commits = {c["prompt_commitment"] for c in spy.calls}
        assert len(commits) == 2
        assert owner.ring_buffers.pow_admission_valid_host[ROW]
        assert owner.ring_buffers.pow_admission_valid_host[ROW2]

    def test_async_row_parking_lifecycle(self, csh_real):
        """begin_admission_for_rows submits in the background and reports the row
        as pending (not yet sampleable); poll_admission stores the nonce and
        clears pending only once the grind finishes."""
        import threading
        owner = _owner()
        gate = threading.Event()

        def slow_grind(msg_w, model_id, target_le, max_tries, commitment):
            gate.wait(5)                      # block the worker until released
            return NONCE

        h = _helper(csh_real, owner, grind_fn=slow_grind)
        pending = h.begin_admission_for_rows(
            [SEQ], [ROW], torch.tensor([0], dtype=torch.int32),
            _ctx_rows([1, 2, 3]), PRECISION)
        assert ROW in pending                          # parked
        assert h.poll_admission() == set()             # still grinding
        assert not owner.ring_buffers.pow_admission_valid_host[ROW]

        gate.set()                                     # let the grind finish
        ready = set()
        for _ in range(500):
            ready = h.poll_admission()
            if ready:
                break
            time.sleep(0.01)
        assert ROW in ready
        assert h.pending_admission_rows() == set()
        assert owner.ring_buffers.pow_admission_valid_host[ROW]
        assert bytes(owner.ring_buffers.pow_admission_nonce[ROW].tolist()) == NONCE

    def test_non_boundary_rows_do_not_grind(self, csh_real):
        owner = _owner()
        spy = _GrindSpy()
        h = _helper(csh_real, owner, grind_fn=spy)
        h.ensure_admission_for_rows([SEQ], [ROW],
                                    torch.tensor([7], dtype=torch.int32),
                                    _ctx_rows([1, 2, 3]), PRECISION)
        assert spy.calls == []
        assert not owner.ring_buffers.pow_admission_valid_host[ROW]

    def test_mode_off_never_grinds(self, csh_real):
        owner = _owner()
        spy = _GrindSpy()
        h = _helper(csh_real, owner, mode="off", grind_fn=spy)
        h.ensure_admission_for_rows([SEQ], [ROW],
                                    torch.tensor([0], dtype=torch.int32),
                                    _ctx_rows([1, 2, 3]), PRECISION)
        assert spy.calls == []

    def test_v2_proof_version_never_grinds(self, csh_real):
        owner = _owner()
        owner.proof_writer.proof_version = 2
        spy = _GrindSpy()
        h = _helper(csh_real, owner, grind_fn=spy)
        h.ensure_admission_for_rows([SEQ], [ROW],
                                    torch.tensor([0], dtype=torch.int32),
                                    _ctx_rows([1, 2, 3]), PRECISION)
        assert spy.calls == []


class TestPerWindowLifecycle:
    def test_boundary_refreshes_nonce_per_window(self, csh_real):
        # A nonce admits exactly ONE window: at the next boundary the stale
        # nonce is cleared first, and the grind sees the NEW msg_w/commitment.
        owner = _owner()
        spy = _GrindSpy()
        h = _helper(csh_real, owner, grind_fn=spy)
        h.ensure_admission_for_window(SEQ, ROW, [1, 2, 3], 0, PRECISION)
        assert owner.ring_buffers.pow_admission_valid_host[ROW]

        # window done: archive grew by 256 generated tokens
        gen = list(range(1000, 1000 + POW_WINDOW_SIZE))
        owner.seq_caches[SEQ]["archive_list"].extend(gen)
        owner.seq_caches[SEQ]["pad_mask_list"].extend([False] * len(gen))
        new_ctx = ([1, 2, 3] + gen)[-POW_WINDOW_SIZE:]
        h.ensure_admission_for_window(SEQ, ROW, new_ctx, 0, PRECISION)

        assert len(spy.calls) == 2
        assert spy.calls[0]["msg_w"] != spy.calls[1]["msg_w"]
        assert (spy.calls[0]["prompt_commitment"]
                != spy.calls[1]["prompt_commitment"])
        assert spy.calls[1]["prompt_commitment"] == pow_v3.prompt_commitment(
            [1, 2, 3] + gen, [False] * (3 + len(gen)))

    def test_grind_miss_leaves_row_nonce_less(self, csh_real):
        owner = _owner()
        h = _helper(csh_real, owner, grind_fn=_GrindSpy(result=None))
        # pre-stain the row to prove the stale nonce is CLEARED even on miss
        owner.ring_buffers.write_admission_nonce(ROW, NONCE)
        ok = h.ensure_admission_for_window(SEQ, ROW, [1, 2, 3], 0, PRECISION)
        assert not ok
        assert not owner.ring_buffers.pow_admission_valid_host[ROW]
        assert bytes(owner.ring_buffers.pow_admission_nonce[ROW].tolist()) == bytes(32)

    def test_missing_snapshot_or_difficulty_skips(self, csh_real):
        owner = _owner()
        spy = _GrindSpy()
        h = _helper(csh_real, owner, grind_fn=spy)
        owner.seq_params[SEQ]["pow_snapshot"]["difficulty"] = 0
        assert not h.ensure_admission_for_window(SEQ, ROW, [1], 0, PRECISION)
        owner.seq_params[SEQ] = {}
        assert not h.ensure_admission_for_window(SEQ, ROW, [1], 0, PRECISION)
        assert spy.calls == []

    def test_no_native_grinder_mines_nonce_less(self, csh_real):
        owner = _owner()
        h = _helper(csh_real, owner)          # no injected grind fn
        h._admission_grind_resolved = True    # simulate: no module found
        h._admission_grind_fn = None
        assert not h.ensure_admission_for_window(SEQ, ROW, [1, 2, 3], 0,
                                                 PRECISION)
        assert not owner.ring_buffers.pow_admission_valid_host[ROW]


class TestStartupSelfTest:
    def test_passes_with_working_grinder(self, csh_real):
        owner = _owner()
        h = _helper(csh_real, owner, grind_fn=_GrindSpy())
        h.assert_v3_ready()               # must not raise

    def test_fails_without_native_grinder(self, csh_real):
        owner = _owner()
        h = _helper(csh_real, owner)
        h._admission_grind_resolved = True
        h._admission_grind_fn = None
        with pytest.raises(RuntimeError, match="admission_grind is unavailable"):
            h.assert_v3_ready()

    def test_fails_when_grinder_finds_nothing(self, csh_real):
        owner = _owner()
        h = _helper(csh_real, owner, grind_fn=_GrindSpy(result=None))
        with pytest.raises(RuntimeError, match="found no nonce"):
            h.assert_v3_ready()

    def test_fails_when_grinder_raises(self, csh_real):
        owner = _owner()

        def broken(*a, **k):
            raise RuntimeError("argon2 backend not compiled in")

        h = _helper(csh_real, owner, grind_fn=broken)
        with pytest.raises(RuntimeError, match="self-test"):
            h.assert_v3_ready()

    def test_default_mode_is_always(self, csh_real):
        owner = _owner()
        os.environ.pop("POW_V3_ADMISSION_MODE", None)
        h = csh_real.CommonSamplerHelper(owner, proxy_audit_enabled=False)
        assert h.admission_mode == "always"


@pytest.mark.skipif(not HAVE_ARGON2, reason="argon2-cffi unavailable")
class TestEndToEnd:
    def test_ground_nonce_is_admissible_and_binds_sampling(self, csh_real):
        """Full loop with a REAL (python-argon2) grinder against an easy
        target: the stored nonce satisfies the admission puzzle for exactly
        the msg_w that batch_sample_tokens hashes, and sampling appends it."""
        easy_difficulty = 5 * 10**13          # expected_tries = 1

        def py_grind(msg_w, mid, target_le, max_tries, commitment):
            target = int.from_bytes(target_le, "little")
            for i in range(int(max_tries)):
                cand = i.to_bytes(32, "little")
                d = pow_v3.argon2id_digest(
                    pow_v3.admission_message(msg_w, mid, cand, commitment))
                if pow_v3.admission_valid(d, target):
                    return cand
            return None

        owner = _owner()
        owner.seq_params[SEQ]["pow_snapshot"]["difficulty"] = easy_difficulty
        owner.ring_buffers.write_pow_params(
            ROW, owner.seq_params[SEQ]["pow_snapshot"])
        h = _helper(csh_real, owner, grind_fn=py_grind)

        ctx = [1, 2, 3]
        ok = h.ensure_admission_for_window(SEQ, ROW, [0] * (POW_WINDOW_SIZE - 3) + ctx,
                                           0, PRECISION)
        assert ok
        nonce = bytes(owner.ring_buffers.pow_admission_nonce[ROW].tolist())

        # sampling at the window-first step appends exactly this nonce
        ctx_rows = _ctx_rows(ctx)
        cdfs = torch.linspace(0, 1, 101).unsqueeze(0)
        _, _, digests = owner.pow_hasher.batch_sample_tokens(
            ctx_rows, torch.tensor([0], dtype=torch.int32), cdfs, PRECISION,
            ring_buffers=owner.ring_buffers,
            rows_tensor=torch.tensor([ROW]), rows_host=[ROW])
        expected = pow_v3.step_u_from_message(pow_v3.build_step_message(
            HEADER, VDF, TICK, 0, ctx, PRECISION, admission_nonce=nonce))[1]
        assert bytes(digests[0].tolist()) == expected

        # and the nonce is genuinely admissible for that window's puzzle
        msg_w = pow_v3.build_step_message(HEADER, VDF, TICK, 0, ctx, PRECISION)
        commitment = pow_v3.prompt_commitment([1, 2, 3], [False] * 3)
        d = pow_v3.argon2id_digest(
            pow_v3.admission_message(msg_w, MODEL_ID, nonce, commitment))
        assert pow_v3.admission_valid(
            d, pow_v3.admission_target(easy_difficulty))

    def test_512_generation_pays_admission_once_per_window(self, csh_real):
        """Simulate one 512-token generation on one row with real Argon2
        admission grinding. The miner must grind exactly at step 0 and step
        256, store the selected nonce before each window's first sample, and
        append that window's nonce to every sampled u preimage."""
        easy_difficulty = 5 * 10**13          # expected_tries = 1
        calls = []
        argon_evals = 0

        def py_grind(msg_w, mid, target_le, max_tries, commitment):
            nonlocal argon_evals
            target = int.from_bytes(target_le, "little")
            call = dict(msg_w=bytes(msg_w),
                        model_identifier=mid,
                        target_le=bytes(target_le),
                        max_tries=int(max_tries),
                        prompt_commitment=bytes(commitment),
                        argon_start=argon_evals)
            for i in range(int(max_tries)):
                argon_evals += 1
                cand = i.to_bytes(32, "little")
                d = pow_v3.argon2id_digest(
                    pow_v3.admission_message(msg_w, mid, cand, commitment))
                if pow_v3.admission_valid(d, target):
                    call["argon_end"] = argon_evals
                    calls.append(call)
                    return cand
            call["argon_end"] = argon_evals
            calls.append(call)
            return None

        owner = _owner()
        owner.seq_params[SEQ]["pow_snapshot"]["difficulty"] = easy_difficulty
        owner.ring_buffers.write_pow_params(
            ROW, owner.seq_params[SEQ]["pow_snapshot"])
        h = _helper(csh_real, owner, grind_fn=py_grind)

        cdfs = torch.linspace(0, 1, 101).unsqueeze(0)
        window_nonces = {}
        boundary_commitments = []
        archive = owner.seq_caches[SEQ]["archive_list"]
        pad_mask = owner.seq_caches[SEQ]["pad_mask_list"]

        for generated in range(512):
            step = generated % POW_WINDOW_SIZE
            ctx = archive[-POW_WINDOW_SIZE:]
            ctx_rows = _ctx_rows(ctx)

            if step == 0:
                boundary_commitments.append(pow_v3.prompt_commitment(
                    archive, pad_mask))
                owner.ring_buffers.steps[ROW] = generated
                h.ensure_admission_for_rows(
                    [SEQ], [ROW], torch.tensor([step], dtype=torch.int32),
                    ctx_rows, PRECISION)
                assert owner.ring_buffers.pow_admission_valid_host[ROW]
                window_nonces[generated // POW_WINDOW_SIZE] = bytes(
                    owner.ring_buffers.pow_admission_nonce[ROW].tolist())

            nonce = bytes(owner.ring_buffers.pow_admission_nonce[ROW].tolist())
            assert nonce == window_nonces[generated // POW_WINDOW_SIZE]

            _, _, digests = owner.pow_hasher.batch_sample_tokens(
                ctx_rows, torch.tensor([step], dtype=torch.int32), cdfs,
                PRECISION, ring_buffers=owner.ring_buffers,
                rows_tensor=torch.tensor([ROW]), rows_host=[ROW])
            expected = pow_v3.step_u_from_message(pow_v3.build_step_message(
                HEADER, VDF, TICK, step, ctx, PRECISION,
                admission_nonce=nonce))[1]
            assert bytes(digests[0].tolist()) == expected

            # Advance the model-visible prefix exactly once per sampled token.
            archive.append(10_000 + generated)
            pad_mask.append(False)

        assert len(calls) == 2
        assert calls[0]["msg_w"] != calls[1]["msg_w"]
        assert calls[0]["prompt_commitment"] == boundary_commitments[0]
        assert calls[1]["prompt_commitment"] == boundary_commitments[1]
        assert calls[0]["prompt_commitment"] != calls[1]["prompt_commitment"]
        assert calls[0]["argon_end"] > calls[0]["argon_start"]
        assert calls[1]["argon_end"] > calls[1]["argon_start"]
        assert argon_evals == (calls[0]["argon_end"] - calls[0]["argon_start"]
                               + calls[1]["argon_end"] - calls[1]["argon_start"])
