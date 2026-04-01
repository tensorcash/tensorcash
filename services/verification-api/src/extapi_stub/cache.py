# SPDX-License-Identifier: Apache-2.0
"""
LRU result cache with TTL, in-flight request coalescing, and pending
lifecycle tracking for async /submit endpoints.

Cache keys include pow_blob_hash (truncated to 16 chars) for full/pow
verification to prevent collisions across different pow blobs.

Configuration (env vars):
    VERIFY_CACHE_ENABLED       — enable/disable (default: true)
    VERIFY_CACHE_TTL_SECONDS   — time-to-live per entry (default: 300)
    VERIFY_CACHE_MAX_SIZE      — max cached results (default: 10000)
"""

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("VERIFY_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
_TTL = int(os.environ.get("VERIFY_CACHE_TTL_SECONDS", "300"))
_MAX_SIZE = int(os.environ.get("VERIFY_CACHE_MAX_SIZE", "10000"))


def make_cache_key(
    verification_type: str,
    hash_id: str,
    pow_blob_hash: Optional[str] = None,
) -> str:
    """
    Build a cache key matching gateway semantics.

    For full/pow verification, includes the first 16 chars of
    pow_blob_hash so that different pow blobs with the same hash_id
    don't collide.
    """
    if pow_blob_hash and verification_type in ("full", "pow"):
        short = pow_blob_hash[:16] if len(pow_blob_hash) > 16 else pow_blob_hash
        return f"{verification_type}:{hash_id}:{short}"
    return f"{verification_type}:{hash_id}"


class VerificationCache:
    """Thread-safe LRU cache with TTL, request coalescing, and pending tracking."""

    def __init__(self, max_size: int = _MAX_SIZE, ttl: int = _TTL, enabled: bool = _ENABLED):
        self.max_size = max_size
        self.ttl = ttl
        self.enabled = enabled

        # cache: key → (result_dict, timestamp)
        self._cache: OrderedDict[str, Tuple[dict, float]] = OrderedDict()
        self._lock = threading.Lock()

        # secondary index: hash_id → set of cache_keys (for status lookup by hash alone)
        self._hash_index: Dict[str, Set[str]] = {}

        # in-flight dedup: key → asyncio.Future
        self._inflight: Dict[str, asyncio.Future] = {}
        self._inflight_lock = threading.Lock()

        # pending lifecycle: cache_key → {"state": "pending"|"computing", "ts": float}
        self._pending: Dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # stats
        self.hits = 0
        self.misses = 0
        self.coalesced = 0

    # ------------------------------------------------------------------ #
    # Cache CRUD
    # ------------------------------------------------------------------ #

    def get(self, key: str) -> Optional[dict]:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.misses += 1
                return None
            result, ts = entry
            if time.monotonic() - ts > self.ttl:
                self._cache.pop(key, None)
                self.misses += 1
                return None
            self._cache.move_to_end(key)
            self.hits += 1
            return result

    def get_by_hash_id(self, hash_id: str, verification_type: Optional[str] = None) -> Optional[dict]:
        """Lookup by hash_id, optionally filtered by type."""
        if not self.enabled:
            return None
        with self._lock:
            keys = list(self._hash_index.get(hash_id, set()))
        for key in keys:
            if verification_type and not key.startswith(f"{verification_type}:"):
                continue
            result = self.get(key)
            if result is not None:
                return result
        return None

    def put(self, key: str, result: dict, hash_id: Optional[str] = None):
        if not self.enabled:
            return
        with self._lock:
            self._cache[key] = (result, time.monotonic())
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)
            if hash_id:
                self._hash_index.setdefault(hash_id, set()).add(key)
        # Clear pending lifecycle on successful cache write
        with self._pending_lock:
            self._pending.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._hash_index.clear()
        with self._pending_lock:
            self._pending.clear()

    # ------------------------------------------------------------------ #
    # Pending lifecycle (for /submit async endpoints)
    # ------------------------------------------------------------------ #

    def mark_pending(self, key: str, accepted_at: Optional[float] = None, state: str = "pending"):
        """Mark a cache key as pending (async submit accepted)."""
        with self._pending_lock:
            self._pending[key] = {"state": state, "ts": accepted_at or time.time()}

    def mark_computing(self, key: str):
        """Transition from pending → computing."""
        with self._pending_lock:
            entry = self._pending.get(key)
            if entry:
                entry["state"] = "computing"
            else:
                self._pending[key] = {"state": "computing", "ts": time.monotonic()}

    def clear_pending(self, key: str):
        with self._pending_lock:
            self._pending.pop(key, None)

    def get_pending(self, key: str) -> Optional[dict]:
        with self._pending_lock:
            return self._pending.get(key)

    def is_pending_or_inflight(self, key: str) -> bool:
        """Check whether a given cache key has pending lifecycle or in-flight future."""
        with self._pending_lock:
            if key in self._pending:
                return True
        with self._inflight_lock:
            fut = self._inflight.get(key)
            if fut is not None and not fut.done():
                return True
        return False

    def has_inflight_for_hash(self, hash_id: str, verification_type: Optional[str] = None) -> bool:
        """Check whether any in-flight or pending state exists for a hash_id."""
        prefix = f"{verification_type}:{hash_id}" if verification_type else hash_id
        with self._inflight_lock:
            for key in self._inflight:
                if key == prefix or key.startswith(prefix + ":") or key.startswith(f"{prefix}"):
                    fut = self._inflight[key]
                    if not fut.done():
                        return True
        with self._pending_lock:
            for key in self._pending:
                if key == prefix or key.startswith(prefix + ":") or key.startswith(f"{prefix}"):
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Request coalescing (dedup for sync endpoints)
    # ------------------------------------------------------------------ #

    def get_or_create_inflight(self, key: str, loop: asyncio.AbstractEventLoop) -> Tuple[asyncio.Future, bool]:
        with self._inflight_lock:
            existing = self._inflight.get(key)
            if existing is not None and not existing.done():
                self.coalesced += 1
                return existing, False
            fut = loop.create_future()
            self._inflight[key] = fut
            return fut, True

    def resolve_inflight(self, key: str, result: dict, hash_id: Optional[str] = None):
        self.put(key, result, hash_id=hash_id)
        with self._inflight_lock:
            fut = self._inflight.pop(key, None)
        if fut and not fut.done():
            fut.get_loop().call_soon_threadsafe(fut.set_result, result)

    def reject_inflight(self, key: str, exc: Exception):
        self.clear_pending(key)
        with self._inflight_lock:
            fut = self._inflight.pop(key, None)
        if fut and not fut.done():
            fut.get_loop().call_soon_threadsafe(fut.set_exception, exc)

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "size": len(self._cache),
            "max_size": self.max_size,
            "ttl": self.ttl,
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
            "inflight": len(self._inflight),
            "pending": len(self._pending),
        }


# Singleton
_cache = VerificationCache()


def get_cache() -> VerificationCache:
    return _cache
