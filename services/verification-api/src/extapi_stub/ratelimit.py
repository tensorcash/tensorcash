# SPDX-License-Identifier: Apache-2.0
"""
Token-bucket rate limiter for the external verification API.

Default: 5 000 requests per minute, shared across all clients.
Per-key limiting can be enabled by setting VERIFY_RATE_LIMIT_PER_KEY=true.

Configuration (env vars):
    VERIFY_RATE_LIMIT_ENABLED   — enable/disable (default: true)
    VERIFY_RATE_LIMIT_PER_MIN   — requests per minute (default: 5000)
    VERIFY_RATE_LIMIT_BURST     — burst size (default: same as per-min)
    VERIFY_RATE_LIMIT_PER_KEY   — per-key buckets (default: false → global)
"""

import os
import time
import logging
import threading
from typing import Dict, Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("VERIFY_RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes")
_PER_MIN = int(os.environ.get("VERIFY_RATE_LIMIT_PER_MIN", "5000"))
_BURST = int(os.environ.get("VERIFY_RATE_LIMIT_BURST", str(_PER_MIN)))
_PER_KEY = os.environ.get("VERIFY_RATE_LIMIT_PER_KEY", "false").lower() in ("1", "true", "yes")

# Paths exempt from rate limiting
_EXEMPT_PATHS = frozenset({
    "/health",
    "/healthz",
    "/readyz",
    "/v1/verify/health",
})


class _TokenBucket:
    """Thread-safe token bucket."""

    __slots__ = ("rate", "burst", "tokens", "last_refill", "lock")

    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = rate_per_sec
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def consume(self) -> bool:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class _BucketStore:
    """Manages per-key or global buckets with lazy eviction."""

    def __init__(self, rate_per_sec: float, burst: int, per_key: bool):
        self.rate = rate_per_sec
        self.burst = burst
        self.per_key = per_key
        self._global = _TokenBucket(rate_per_sec, burst)
        self._keyed: Dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()

    def consume(self, key: Optional[str] = None) -> bool:
        if not self.per_key or key is None:
            return self._global.consume()

        with self._lock:
            bucket = self._keyed.get(key)
            if bucket is None:
                bucket = _TokenBucket(self.rate, self.burst)
                self._keyed[key] = bucket
        return bucket.consume()


_store = _BucketStore(
    rate_per_sec=_PER_MIN / 60.0,
    burst=_BURST,
    per_key=_PER_KEY,
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Return 429 when the token bucket is exhausted."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not _ENABLED:
            return await call_next(request)

        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # Extract key from Authorization header (if per-key)
        key: Optional[str] = None
        if _PER_KEY:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:]

        if not _store.consume(key):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Max: {} req/min".format(_PER_MIN),
            )

        return await call_next(request)
