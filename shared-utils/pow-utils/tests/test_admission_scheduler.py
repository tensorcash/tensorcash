"""AdmissionScheduler — scheduler-side async admission driver (row parking)."""
import threading
import time

import pow_v3
from admission_scheduler import AdmissionScheduler, PASS, PARK, READY

HEADER = bytes(range(64))
VDF = bytes(range(100, 132))
NONCE = bytes(range(32))
W = 4                     # tiny window for easy boundaries
PREC = "fp16"
MODEL = "org/model@abcdef012345"
POW = {"header_prefix": HEADER.hex(), "vdf": VDF.hex(), "tick": 42,
       "difficulty": 1_000_000, "block_hash": "bb" * 32}


class _Spy:
    def __init__(self, result=NONCE):
        self.result = result
        self.calls = []

    def __call__(self, msg_w, model_id, target_le, max_tries, commitment):
        self.calls.append((bytes(msg_w), model_id, bytes(target_le),
                           int(max_tries), bytes(commitment)))
        return self.result


def _sched(grind_fn, mode="always", **kw):
    return AdmissionScheduler(grind_fn, precision=PREC, model_identifier=MODEL,
                              window_size=W, admission_mode=mode, **kw)


def _wait_ready(s, req, w, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if s._grinder.is_ready(req, w):
            return True
        time.sleep(0.005)
    return False


def _wait_calls(spy, n, timeout=5):
    """Grinds run on worker threads, so the spy's record lands asynchronously and
    in nondeterministic order — wait for n calls before inspecting them."""
    end = time.time() + timeout
    while time.time() < end:
        if len(spy.calls) >= n:
            return
        time.sleep(0.005)


def _expected(prefix_tokens):
    return pow_v3.build_admission_preimage(
        header_prefix=HEADER, vdf=VDF, tick=42, step=0,
        context_tokens=prefix_tokens[-W:], prefix_tokens=prefix_tokens,
        prefix_pad_mask=[False] * len(prefix_tokens), precision=PREC,
        difficulty=1_000_000, model_identifier=MODEL, max_tries_factor=1)


# --- no-op invariant --------------------------------------------------------
def test_noop_when_admission_off():
    spy = _Spy()
    s = _sched(spy, mode="off")
    s.on_request_start("r", [1, 2, 3, 4, 5], POW)
    s.on_output_progress("r", [1, 2, 3, 4, 5, 6], 5, POW)
    assert spy.calls == []
    assert not s.has_pending()
    assert s.gate("r", 0, POW) == PASS


def test_noop_when_no_pow_or_zero_difficulty():
    spy = _Spy()
    s = _sched(spy)
    s.on_request_start("r", [1, 2, 3], None)
    s.on_request_start("r", [1, 2, 3], {**POW, "difficulty": 0})
    assert spy.calls == [] and not s.has_pending()


# --- submission windowing ---------------------------------------------------
def test_window0_submitted_from_prompt_tail():
    spy = _Spy()
    s = _sched(spy)
    prompt = [10, 11, 12, 13, 14, 15]         # len 6 > W
    s.on_request_start("r", prompt, POW)
    assert s._grinder.is_pending("r", 0)
    _wait_calls(spy, 1)
    assert len(spy.calls) == 1
    assert spy.calls[0] == _expected(prompt)   # ctx = last W of prompt, prefix = prompt


def test_next_window_submitted_when_boundary_commits():
    spy = _Spy()
    s = _sched(spy)
    prompt = [10, 11]
    s.on_request_start("r", prompt, POW)                    # window 0
    # generate W output tokens -> window 1's context (prompt+out[:W]) is complete
    out = [20, 21, 22, 23]
    s.on_output_progress("r", prompt + out, len(prompt), POW)
    assert s._grinder.is_pending("r", 1)
    _wait_calls(spy, 2)                                      # window 0 + window 1
    assert _expected(prompt + out) in spy.calls             # prefix = prompt+out[:W]


def test_catch_up_multiple_windows_one_progress():
    spy = _Spy()
    s = _sched(spy)
    prompt = [9]
    s.on_request_start("r", prompt, POW)                    # window 0
    full = prompt + list(range(100, 100 + 3 * W))           # 3 windows of output
    s.on_output_progress("r", full, len(prompt), POW)
    # windows 1,2,3 now context-complete
    for w in (1, 2, 3):
        assert s._grinder.is_pending("r", w)


# --- gate lifecycle ---------------------------------------------------------
def test_gate_pass_mid_window():
    s = _sched(_Spy())
    assert s.gate("r", 2, POW) == PASS         # 2 % 4 != 0


def test_gate_park_then_ready_then_take():
    gate = threading.Event()

    def slow(*a):
        gate.wait(5)
        return NONCE

    s = _sched(slow)
    s.on_request_start("r", [1, 2, 3, 4], POW)     # submits window 0
    assert s.gate("r", 0, POW) == PARK             # grinding
    gate.set()
    assert _wait_ready(s, "r", 0)
    assert s.gate("r", 0, POW) == READY
    assert s.take_nonce("r", 0) == NONCE
    # taken -> no longer pending
    assert not s._grinder.is_pending("r", 0)


# --- byte-equivalence with the shared builder (== sampler path) -------------
def test_submitted_preimage_matches_shared_builder():
    spy = _Spy()
    s = _sched(spy)
    prompt = [7, 8, 9, 10, 11]
    out = [30, 31, 32, 33]
    s.on_request_start("r", prompt, POW)
    s.on_output_progress("r", prompt + out, len(prompt), POW)
    _wait_calls(spy, 2)
    # windows grind concurrently -> order is nondeterministic; compare as a set
    assert set(spy.calls) == {_expected(prompt), _expected(prompt + out)}


# --- nonce-less boundary (positive difficulty, un-buildable preimage) --------
def test_malformed_pow_positive_difficulty_gates_ready_nonce_less():
    """A request that is admission-active (difficulty>0) but whose preimage cannot
    be built (e.g. missing/garbage vdf) must NOT park forever: the boundary resolves
    nonce-less — gate()==READY and take_nonce()==None — not PARK."""
    spy = _Spy()
    s = _sched(spy)
    # positive difficulty -> active(); but vdf is not valid hex -> _build() returns None
    bad = {**POW, "vdf": "zznothex"}
    s.on_request_start("r", [1, 2, 3, 4], bad)
    assert spy.calls == []                          # never handed to the grinder
    assert not s._grinder.is_pending("r", 0)        # no future exists
    assert s.gate("r", 0, bad) == READY             # but the boundary can proceed
    assert s.take_nonce("r", 0) is None             # mines nonce-less
    # consumed exactly once
    assert ("r", 0) not in s._nonce_less


def test_finish_clears_nonce_less():
    s = _sched(_Spy())
    bad = {**POW, "vdf": "zznothex"}
    s.on_request_start("r", [1, 2, 3, 4], bad)
    assert ("r", 0) in s._nonce_less
    s.on_request_finished("r")
    assert ("r", 0) not in s._nonce_less


# --- grind-error policy -----------------------------------------------------
def test_take_nonce_grind_error_logged_not_crash():
    class _Log:
        def __init__(self): self.msgs = []
        def log(self, m, lvl="INFO"): self.msgs.append((lvl, m))

    def boom(*a):
        raise RuntimeError("kaboom")

    log = _Log()
    s = _sched(boom, logger=log, crash_on_grind_error=False)
    s.on_request_start("r", [1, 2, 3, 4], POW)
    assert _wait_ready(s, "r", 0)
    assert s.gate("r", 0, POW) == READY
    assert s.take_nonce("r", 0) is None                   # logged, not raised
    assert any(lvl == "ERROR" for lvl, _ in log.msgs)
    assert not s._grinder.is_pending("r", 0)              # not parked forever


def test_take_nonce_grind_error_crashes_in_dev():
    def boom(*a):
        raise RuntimeError("kaboom")

    s = _sched(boom, crash_on_grind_error=True)
    s.on_request_start("r", [1, 2, 3, 4], POW)
    assert _wait_ready(s, "r", 0)
    try:
        s.take_nonce("r", 0)
        assert False, "expected crash in dev mode"
    except RuntimeError:
        pass


# --- invalidate -------------------------------------------------------------
def test_finish_invalidates_pending():
    gate = threading.Event()

    def slow(*a):
        gate.wait(5)
        return NONCE

    s = _sched(slow)
    s.on_request_start("r", [1, 2, 3, 4], POW)
    assert s._grinder.is_pending("r", 0)
    s.on_request_finished("r")
    assert not s._grinder.is_pending("r", 0)
    assert s._next_window.get("r") is None
    gate.set()
