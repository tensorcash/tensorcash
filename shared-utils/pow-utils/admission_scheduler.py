"""Scheduler-side driver for async v3 admission (row parking, TIP-0003
§6/§9). Turns a request's token/pow state into background grind submissions and
per-tick schedule decisions, keyed by DURABLE identity (request_id, window_index)
via :class:`AdmissionGrinder`, and builds every preimage through the single shared
source :func:`pow_v3.build_admission_preimage` so the bytes match the sampler's.

Deliberately vLLM-agnostic: the methods take plain token lists + the request's pow
dict, so this is unit-testable without an engine. The scheduler adapts its Request
objects to these calls.

No-op invariant: unless a request is admission-active (POW_V3_ADMISSION_MODE=always
AND a v3 pow payload with a positive difficulty), every entry point is an O(1)
early return and no grind, future, or boundary math happens.

Window model (matches the sampler's `steps % window == 0` boundary): a window is
`window_size` GENERATED tokens. Window w's first token is at output index w*W; its
nonce binds to the rolling context (last W of prompt+output up to that index) and
the pre-window prefix, both finalized exactly when output length reaches w*W — so
window 0 is submittable at prefill (prompt-only) and window w>=1 the moment its
boundary token commits.
"""
import os

try:
    from vllm.sampling import pow_v3
except ImportError:
    import pow_v3

from admission_grinder import AdmissionGrinder

# gate() decisions
PASS = "pass"     # not admission-active, or mid-window: schedule normally
PARK = "park"     # at a boundary whose nonce is not ready: skip this tick
READY = "ready"   # at a boundary whose nonce is ready: take_nonce() then schedule


class AdmissionScheduler:
    def __init__(self, grind_fn, *, precision, model_identifier,
                 window_size=256, max_tries_factor=1, admission_mode=None,
                 logger=None, crash_on_grind_error=None):
        self._grinder = AdmissionGrinder(grind_fn, logger=logger)
        self._precision = precision
        self._model_id = model_identifier
        self._W = int(window_size)
        self._factor = int(max_tries_factor)
        self._mode = (admission_mode
                      if admission_mode is not None
                      else os.environ.get("POW_V3_ADMISSION_MODE", "off"))
        self._log = logger
        if crash_on_grind_error is None:
            crash_on_grind_error = os.environ.get(
                "POW_V3_ADMISSION_DEV_CRASH", "0") in ("1", "true", "True")
        self._crash = crash_on_grind_error
        # request_id -> next window index whose grind has not been submitted yet
        self._next_window = {}
        # (request_id, window) boundaries that resolved to nonce-less at submit time
        # (positive difficulty but preimage un-buildable) — no future exists, so
        # gate() must learn READY from here or it would PARK the row forever.
        self._nonce_less = set()

    # -- activation gate (the no-op guard) ---------------------------------- #
    def active(self, pow_dict):
        """True iff this request needs admission grinds. Cheapest possible check;
        everything else short-circuits on it."""
        if self._mode != "always" or not pow_dict:
            return False
        try:
            return int(pow_dict.get("difficulty")) > 0
        except (TypeError, ValueError):
            return False

    def has_pending(self):
        """Any grind in flight — lets the scheduler skip the whole boundary block."""
        return self._grinder.has_pending()

    # -- submission driven by request lifecycle ----------------------------- #
    def on_request_start(self, request_id, prompt_token_ids, pow_dict):
        """Prefill: submit window 0 (prompt-derived) so it grinds while the GPU
        prefills. No-op if not admission-active."""
        if not self.active(pow_dict):
            return
        self._next_window[request_id] = 0
        self._submit_ready_windows(request_id, list(prompt_token_ids),
                                   num_output=0, pow_dict=pow_dict)

    def on_output_progress(self, request_id, all_token_ids, num_prompt_tokens,
                           pow_dict):
        """After a commit: submit any window whose boundary context just became
        complete (output length reached w*W). No-op if not admission-active."""
        if not self.active(pow_dict):
            return
        num_output = len(all_token_ids) - int(num_prompt_tokens)
        self._submit_ready_windows(request_id, list(all_token_ids),
                                   num_output=num_output, pow_dict=pow_dict)

    def _submit_ready_windows(self, request_id, all_token_ids, num_output,
                              pow_dict):
        nxt = self._next_window.get(request_id, 0)
        # a window w is context-complete once num_output >= w*W (window 0 always)
        while nxt * self._W <= num_output:
            boundary = nxt * self._W
            prefix = all_token_ids[:len(all_token_ids) - (num_output - boundary)]
            job = self._build(prefix, pow_dict)
            if job is None:                       # un-buildable preimage -> nonce-less
                # Record it: no future will ever be ready for this boundary, so gate()
                # reads READY from _nonce_less instead of parking the row forever.
                self._nonce_less.add((request_id, nxt))
                self._next_window[request_id] = nxt + 1
                nxt += 1
                continue
            self._grinder.submit(request_id, nxt, *job)
            nxt += 1
        self._next_window[request_id] = nxt

    # -- per-tick schedule decision ----------------------------------------- #
    def gate(self, request_id, num_output, pow_dict):
        """Decide whether a running request may be scheduled this tick.
        PASS (schedule normally), PARK (skip; nonce not ready), or READY (nonce
        ready — caller must take_nonce() and hand it to the worker)."""
        if not self.active(pow_dict):
            return PASS
        if num_output % self._W != 0:            # mid-window: nonce already in ring
            return PASS
        w = num_output // self._W
        if (request_id, w) in self._nonce_less:  # un-buildable preimage: proceed w/o nonce
            return READY
        if self._grinder.is_ready(request_id, w):
            return READY
        return PARK                              # pending (or not yet submitted)

    def take_nonce(self, request_id, num_output):
        """Pop the ready nonce for this boundary window. Returns bytes, or None if
        the window mines nonce-less (grind exhausted, or errored under the
        log-and-continue policy). Callers should only call this after gate()==READY."""
        w = num_output // self._W
        if (request_id, w) in self._nonce_less:  # boundary was un-buildable: mine nonce-less
            self._nonce_less.discard((request_id, w))
            return None
        try:
            return self._grinder.take(request_id, w)
        except Exception as exc:                 # worker grind raised
            if self._crash:
                raise
            if self._log is not None:
                self._log.log(
                    f"v3 admission grind errored for req {request_id} window {w}: "
                    f"{exc!r} — window mines nonce-less", "ERROR")
            return None

    def on_request_finished(self, request_id):
        """Abort/free/reallocation: drop every pending window so a recycled id
        cannot pick up a stale nonce."""
        self._grinder.invalidate(request_id)
        self._next_window.pop(request_id, None)
        self._nonce_less = {k for k in self._nonce_less if k[0] != request_id}

    def shutdown(self):
        self._grinder.shutdown()

    # -- internal ----------------------------------------------------------- #
    def _build(self, prefix_tokens, pow_dict):
        """Build the grind 5-tuple for a window from its pre-window prefix, via the
        single shared source. Returns None if a required pow field is missing."""
        try:
            header_hex = pow_dict.get("header_prefix") or pow_dict.get("block_hash")
            ctx_window = prefix_tokens[-self._W:]
            return pow_v3.build_admission_preimage(
                header_prefix=bytes.fromhex(header_hex),
                vdf=bytes.fromhex(pow_dict["vdf"]),
                tick=int(pow_dict["tick"]),
                step=0,
                context_tokens=ctx_window,
                prefix_tokens=prefix_tokens,
                prefix_pad_mask=[False] * len(prefix_tokens),
                precision=self._precision,
                difficulty=int(pow_dict["difficulty"]),
                model_identifier=self._model_id,
                max_tries_factor=self._factor,
            )
        except (KeyError, TypeError, ValueError) as exc:
            if self._log is not None:
                self._log.log(
                    f"v3 admission preimage build failed ({exc!r}) — nonce-less",
                    "WARN")
            return None
