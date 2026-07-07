"""Durable-identity async admission grind manager (TIP-0003).

Owns the background Argon2 admission grinds for the v3 mining scheduler, keyed by
DURABLE identity ``(request_id, window_index)`` — NOT sampler row, which recycles
as sequences finish and would let a recycled row pick up a stale nonce.

The scheduler drives it:
  - ``submit`` a window's grind as soon as its context is known (window 0 from the
    prompt tail at prefill; window N+1 when window N's last token commits);
  - ``is_ready`` to decide whether a boundary request may be scheduled this tick;
  - ``take`` the nonce when it schedules that request (handing it to the worker);
  - ``invalidate`` a request on abort/free so its id cannot pick up a stale nonce.

Inert until used: no pool thread is created and every query is O(1) while nothing
is submitted, so the non-admission path pays nothing. Grinds run concurrently —
the native admission_grind releases the GIL — bounded to ~physical cores (8 MiB
Argon2 is memory-bandwidth-bound past that).
"""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor


class AdmissionGrinder:
    def __init__(self, grind_fn, max_workers=None, logger=None):
        # grind_fn(msg_w, model_id, target_le, max_tries, commitment) -> bytes|None
        self._grind_fn = grind_fn
        self._logger = logger
        self._max_workers = max_workers
        self._pool = None                       # created lazily on first submit
        self._pending = {}                      # (req_id, win) -> Future[bytes|None]
        self._by_req = {}                       # req_id -> set(win)  (O(1) invalidate)
        self._fp = {}                           # (req_id, win) -> preimage fingerprint

    @staticmethod
    def _fingerprint(msg_w, model_id, target_le, max_tries, commitment):
        h = hashlib.sha256()
        h.update(bytes(msg_w)); h.update(b"\x00")
        h.update(str(model_id).encode()); h.update(b"\x00")
        h.update(bytes(target_le)); h.update(b"\x00")
        h.update(str(int(max_tries)).encode()); h.update(b"\x00")
        h.update(bytes(commitment))
        return h.digest()

    # -- lifecycle ---------------------------------------------------------- #
    def _ensure_pool(self):
        if self._pool is None:
            n = self._max_workers
            if n is None:
                n = self._resolve_threads()
            self._pool = ThreadPoolExecutor(max_workers=n,
                                            thread_name_prefix="v3grind")
        return self._pool

    @staticmethod
    def _resolve_threads():
        """cpu_count-1 by default (leave a core for the engine thread); override
        with POW_V3_GRIND_THREADS. Parsed defensively — bad values fall back."""
        default = max(1, (os.cpu_count() or 2) - 1)
        raw = os.environ.get("POW_V3_GRIND_THREADS")
        if not raw:
            return default
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return default
        return max(1, n)

    def shutdown(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # -- scheduler API ------------------------------------------------------ #
    def submit(self, request_id, window_index, msg_w, model_id, target_le,
               max_tries, commitment):
        """Start the grind for one window. If it is already in flight, this is a
        no-op ONLY when re-offered with the identical preimage (the scheduler
        re-offers a still-parked boundary each tick); a DIFFERENT preimage for the
        same (request_id, window_index) is a bug — a nonce could be reused for the
        wrong message — so it raises hard."""
        key = (request_id, window_index)
        fp = self._fingerprint(msg_w, model_id, target_le, max_tries, commitment)
        if key in self._pending:
            if self._fp.get(key) != fp:
                raise ValueError(
                    f"admission re-submit for {key} with a DIFFERENT preimage "
                    f"(fingerprint mismatch) — a window's preimage is fixed once "
                    f"its context is known; refusing to grind a second message")
            return
        fut = self._ensure_pool().submit(
            self._grind_fn, msg_w, model_id, target_le, max_tries, commitment)
        self._pending[key] = fut
        self._fp[key] = fp
        self._by_req.setdefault(request_id, set()).add(window_index)

    def is_pending(self, request_id, window_index):
        """True iff a grind for this window has been submitted (ready or not)."""
        return (request_id, window_index) in self._pending

    def is_ready(self, request_id, window_index):
        """True iff the grind was submitted AND has finished (safe to take)."""
        fut = self._pending.get((request_id, window_index))
        return fut is not None and fut.done()

    def take(self, request_id, window_index):
        """Pop and return the ground nonce (bytes), or None if the grind exhausted
        its tries (window mines nonce-less). Callers MUST gate on is_ready();
        raises KeyError if never submitted, RuntimeError if taken before ready."""
        key = (request_id, window_index)
        fut = self._pending[key]                        # KeyError if absent
        if not fut.done():
            raise RuntimeError(
                f"take({request_id}, {window_index}) before the grind finished")
        try:
            return fut.result()                         # may re-raise a worker error
        finally:
            self._drop(key)                             # never park forever, even on failure

    def pending_windows(self, request_id):
        """Window indices currently in flight for a request (possibly finished but
        not yet taken)."""
        return set(self._by_req.get(request_id, ()))

    def has_pending(self):
        """True iff any grind is in flight — lets the scheduler short-circuit the
        whole boundary block when no request is admission-active."""
        return bool(self._pending)

    def invalidate(self, request_id):
        """Abort/free/reallocation: cancel and drop every window for this request
        so a recycled id cannot pick up a stale nonce (durable-identity safety)."""
        for w in self._by_req.pop(request_id, ()):
            fut = self._pending.pop((request_id, w), None)
            self._fp.pop((request_id, w), None)
            if fut is not None:
                fut.cancel()

    # -- internal ----------------------------------------------------------- #
    def _drop(self, key):
        self._pending.pop(key, None)
        self._fp.pop(key, None)
        req, w = key
        rest = self._by_req.get(req)
        if rest is not None:
            rest.discard(w)
            if not rest:
                self._by_req.pop(req, None)
