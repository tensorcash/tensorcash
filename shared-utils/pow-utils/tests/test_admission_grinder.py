"""AdmissionGrinder — durable-identity async admission grind manager."""
import threading
import time

from admission_grinder import AdmissionGrinder

ARGS = (b"msg", "org/model@abc", b"\xff" * 32, 4, b"commit")   # msg_w..commitment


def _gated():
    """A grind_fn that blocks on an Event until released, then returns a nonce
    derived from its message so results are distinguishable."""
    gate = threading.Event()

    def fn(msg_w, model_id, target_le, max_tries, commitment):
        gate.wait(5)
        return b"N" + bytes(msg_w)[:31].ljust(31, b"\x00")

    return fn, gate


def _wait_ready(g, req, win, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if g.is_ready(req, win):
            return True
        time.sleep(0.005)
    return False


def test_inert_until_submitted():
    g = AdmissionGrinder(grind_fn=lambda *a: b"x" * 32)
    assert not g.has_pending()
    assert not g.is_pending("r", 0)
    assert not g.is_ready("r", 0)
    assert g.pending_windows("r") == set()
    assert g._pool is None            # no thread pool created on the idle path


def test_submit_ready_take_lifecycle():
    fn, gate = _gated()
    g = AdmissionGrinder(grind_fn=fn)
    g.submit("req-1", 0, *ARGS)
    assert g.has_pending() and g.is_pending("req-1", 0)
    assert not g.is_ready("req-1", 0)         # still grinding
    gate.set()
    assert _wait_ready(g, "req-1", 0)
    nonce = g.take("req-1", 0)
    assert nonce == b"N" + b"msg"[:31].ljust(31, b"\x00")
    # taken -> gone
    assert not g.is_pending("req-1", 0)
    assert not g.has_pending()


def test_durable_key_two_windows_same_request():
    g = AdmissionGrinder(grind_fn=lambda *a: b"n" * 32)
    g.submit("req-1", 0, *ARGS)
    g.submit("req-1", 1, *ARGS)
    assert g.pending_windows("req-1") == {0, 1}
    assert _wait_ready(g, "req-1", 0) and _wait_ready(g, "req-1", 1)
    g.take("req-1", 0)
    assert g.pending_windows("req-1") == {1}   # window 1 still owned


def test_duplicate_submit_is_noop():
    calls = []
    fn, gate = _gated()

    def counting(*a):
        calls.append(1)
        return fn(*a)

    g = AdmissionGrinder(grind_fn=counting)
    g.submit("req-1", 0, *ARGS)
    g.submit("req-1", 0, *ARGS)                 # same (req, win) -> ignored
    gate.set()
    assert _wait_ready(g, "req-1", 0)
    assert len(calls) == 1


def test_invalidate_drops_all_windows():
    fn, gate = _gated()
    g = AdmissionGrinder(grind_fn=fn)
    g.submit("req-1", 0, *ARGS)
    g.submit("req-1", 1, *ARGS)
    g.submit("req-2", 0, *ARGS)
    g.invalidate("req-1")
    assert g.pending_windows("req-1") == set()
    assert not g.is_pending("req-1", 0) and not g.is_pending("req-1", 1)
    assert g.is_pending("req-2", 0)            # other request untouched
    gate.set()


def test_take_before_ready_raises():
    fn, gate = _gated()
    g = AdmissionGrinder(grind_fn=fn)
    g.submit("req-1", 0, *ARGS)
    try:
        g.take("req-1", 0)                      # not done yet
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    finally:
        gate.set()


def test_resubmit_same_preimage_noop_different_preimage_raises():
    fn, gate = _gated()
    g = AdmissionGrinder(grind_fn=fn)
    g.submit("req-1", 0, *ARGS)
    g.submit("req-1", 0, *ARGS)                 # identical preimage -> no-op
    try:
        # same (req,win) but a different commitment -> hard error, not silent reuse
        g.submit("req-1", 0, b"msg", "org/model@abc", b"\xff" * 32, 4, b"OTHER")
        assert False, "expected ValueError on preimage fingerprint mismatch"
    except ValueError:
        pass
    finally:
        gate.set()


def test_take_drops_key_on_worker_exception():
    def boom(*a):
        raise RuntimeError("grind blew up")

    g = AdmissionGrinder(grind_fn=boom)
    g.submit("req-1", 0, *ARGS)
    assert _wait_ready(g, "req-1", 0)           # future is 'done' (failed)
    try:
        g.take("req-1", 0)
        assert False, "expected the worker exception to propagate"
    except RuntimeError:
        pass
    # crucially: the key is gone, so the row is not parked forever
    assert not g.is_pending("req-1", 0)
    assert g.pending_windows("req-1") == set()


def test_bad_thread_env_falls_back(monkeypatch):
    monkeypatch.setenv("POW_V3_GRIND_THREADS", "not-an-int")
    assert AdmissionGrinder._resolve_threads() >= 1
    monkeypatch.setenv("POW_V3_GRIND_THREADS", "0")
    assert AdmissionGrinder._resolve_threads() == 1
