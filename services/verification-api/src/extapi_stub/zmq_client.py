# SPDX-License-Identifier: Apache-2.0
"""
ZMQ client for communicating with the verification engine.

Sends ValidationRequest via PUSH and receives ValidationResponse via PULL.
Matches responses to pending requests by hash_id AND expected response family,
so concurrent full + model requests for the same hash don't cross-satisfy.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

import zmq

from .builders import response_value_to_str

logger = logging.getLogger(__name__)


# Response enum → kind mapping (matches the Verification Service
_EXPECTED_VALUES: Dict[str, FrozenSet[int]] = {
    "full": frozenset({6, 7, 8}),       # Full_Green, Full_Amber, Full_Red
    "pow": frozenset({6, 7, 8}),
    "model": frozenset({9, 10, 13}),      # Model_OK, Model_Fail, Model_Pending_Review
    "quick": frozenset({0, 1}),          # Quick_OK, Quick_Fail
    "quick_smell": frozenset({2, 3, 4, 5}),
    "logits": frozenset({14, 15}),       # Logits_OK, Logits_Fail
}


def _expected_response_values(request_kind: str) -> FrozenSet[int]:
    kind = (request_kind or "full").replace("-", "_").lower()
    return _EXPECTED_VALUES.get(kind, frozenset(range(0, 14)))


@dataclass
class _PendingRequest:
    future: asyncio.Future
    request_kind: str
    expected_values: FrozenSet[int]


class ZmqVerifyClient:
    """
    Async-aware ZMQ client that sends ValidationRequests to the
    verification engine and collects ValidationResponses.

    Architecture:
        PUSH → engine PULL (port 6001)
        PULL ← engine PUSH (port 7001)

    A background thread drains the PULL socket and resolves asyncio
    futures keyed by (hash_id, response family).
    """

    def __init__(
        self,
        push_endpoint: str = "tcp://localhost:6001",
        pull_bind: str = "tcp://*:7001",
        recv_timeout_ms: int = 60_000,
    ):
        self.push_endpoint = push_endpoint
        self.pull_bind = pull_bind
        self.recv_timeout_ms = recv_timeout_ms

        self._ctx: Optional[zmq.Context] = None
        self._push: Optional[zmq.Socket] = None
        self._pull: Optional[zmq.Socket] = None

        # hash_id → list of pending requests (multiple kinds per hash)
        self._pending: Dict[bytes, List[_PendingRequest]] = {}
        self._pending_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._running = False

        # Callback for terminal responses that arrive with no pending future.
        # This happens when operator approves a model review hours after the
        # original request's future expired. The callback caches the result
        # so HTTP status polls pick it up.
        # Signature: on_orphan_response(hash_id_hex: str, status_str: str, enum_val: int)
        self.on_orphan_response: Optional[callable] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._running:
            return
        self._loop = loop
        self._ctx = zmq.Context()

        self._push = self._ctx.socket(zmq.PUSH)
        self._push.setsockopt(zmq.SNDHWM, 2000)
        self._push.setsockopt(zmq.LINGER, 0)
        self._push.connect(self.push_endpoint)
        logger.info("ZMQ PUSH connected to %s", self.push_endpoint)

        self._pull = self._ctx.socket(zmq.PULL)
        self._pull.setsockopt(zmq.RCVHWM, 2000)
        self._pull.setsockopt(zmq.LINGER, 0)
        self._pull.bind(self.pull_bind)
        logger.info("ZMQ PULL bound to %s", self.pull_bind)

        self._running = True
        self._recv_thread = threading.Thread(
            target=self._recv_loop, name="zmq-recv", daemon=True
        )
        self._recv_thread.start()

    def stop(self):
        self._running = False
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)

        for sock in (self._push, self._pull):
            if sock is not None:
                try:
                    sock.setsockopt(zmq.LINGER, 0)
                    sock.close(0)
                except Exception:
                    pass
        if self._ctx is not None:
            try:
                self._ctx.term()
            except Exception:
                pass

        with self._pending_lock:
            for pending_list in self._pending.values():
                for p in pending_list:
                    if not p.future.done():
                        p.future.cancel()
            self._pending.clear()

        logger.info("ZMQ client stopped")

    @property
    def connected(self) -> bool:
        return self._running and self._push is not None

    # ------------------------------------------------------------------ #
    # Send
    # ------------------------------------------------------------------ #

    def send(
        self,
        hash_id: bytes,
        validation_request_bytes: bytes,
        request_kind: str = "full",
    ) -> asyncio.Future:
        """
        Send a ValidationRequest and return a Future that resolves
        when the engine replies with a matching response family.

        Multiple requests for the same hash_id but different kinds
        (e.g. full + model) are tracked independently.
        """
        expected = _expected_response_values(request_kind)
        fut = self._loop.create_future()
        pending = _PendingRequest(future=fut, request_kind=request_kind, expected_values=expected)

        with self._pending_lock:
            # Check for existing pending of the same kind
            existing_list = self._pending.get(hash_id, [])
            for p in existing_list:
                if not p.future.done() and p.request_kind == request_kind:
                    # Coalesce: return the existing future
                    return p.future
            existing_list.append(pending)
            self._pending[hash_id] = existing_list

        try:
            self._push.send(validation_request_bytes, flags=zmq.DONTWAIT)
        except zmq.Again:
            with self._pending_lock:
                self._remove_pending(hash_id, pending)
            fut.set_exception(
                RuntimeError("ZMQ PUSH backpressure — verification engine not draining")
            )
        except Exception as exc:
            with self._pending_lock:
                self._remove_pending(hash_id, pending)
            fut.set_exception(exc)
        return fut

    def _remove_pending(self, hash_id: bytes, target: _PendingRequest):
        """Remove a specific pending request. Caller must hold _pending_lock."""
        lst = self._pending.get(hash_id)
        if lst is None:
            return
        lst[:] = [p for p in lst if p is not target]
        if not lst:
            self._pending.pop(hash_id, None)

    # ------------------------------------------------------------------ #
    # Background receiver
    # ------------------------------------------------------------------ #

    def _recv_loop(self):
        poller = zmq.Poller()
        poller.register(self._pull, zmq.POLLIN)

        while self._running:
            try:
                socks = dict(poller.poll(timeout=500))
            except zmq.ZMQError:
                if not self._running:
                    break
                continue

            if self._pull not in socks:
                continue

            try:
                data = self._pull.recv(flags=zmq.DONTWAIT)
            except zmq.Again:
                continue
            except zmq.ZMQError:
                if not self._running:
                    break
                continue

            try:
                hash_id, enum_val, status_str = self._parse_response(data)
            except Exception:
                logger.exception("Failed to parse ValidationResponse")
                continue

            # Match against pending requests by expected response family
            matched = None
            with self._pending_lock:
                pending_list = self._pending.get(hash_id)
                if pending_list:
                    remaining: List[_PendingRequest] = []
                    for p in pending_list:
                        if p.future.done():
                            continue
                        if matched is None and enum_val in p.expected_values:
                            matched = p
                            continue
                        remaining.append(p)

                    if remaining:
                        self._pending[hash_id] = remaining
                    else:
                        self._pending.pop(hash_id, None)

            if matched is not None and not matched.future.done():
                self._loop.call_soon_threadsafe(matched.future.set_result, status_str)
            elif matched is None:
                # Orphan response — no pending future. For model terminal results
                # (after operator approval), cache via callback so status polls work.
                if self.on_orphan_response is not None and enum_val in (9, 10):  # Model_OK, Model_Fail
                    try:
                        self.on_orphan_response(hash_id.hex(), status_str, enum_val)
                    except Exception:
                        logger.exception("on_orphan_response callback failed")
                else:
                    logger.debug(
                        "Response for hash %s (enum=%d) matched no pending request",
                        hash_id.hex(), enum_val,
                    )

    @staticmethod
    def _parse_response(data: bytes) -> Tuple[bytes, int, str]:
        """Parse a FlatBuffers ValidationResponse into (hash_id, enum_val, status_str)."""
        try:
            from utils.proof import ValidationResponse, ResponseValue
        except ImportError:
            import sys, os
            sys.path.append(
                os.path.join(os.path.dirname(__file__), "../../../../shared-utils/fb-schemas")
            )
            from proof import ValidationResponse, ResponseValue

        vr = ValidationResponse.ValidationResponse.GetRootAs(data, 0)
        enum_val = vr.EnumResponse()
        status_str = response_value_to_str(enum_val)

        length = vr.HashIdentifierLength()
        if length > 0:
            try:
                hash_id = bytes(vr.HashIdentifierAsNumpy().tobytes())
            except (AttributeError, TypeError):
                hash_id = bytes(vr.HashIdentifier(i) for i in range(length))
        else:
            hash_id = b""

        return hash_id, enum_val, status_str
