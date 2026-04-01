import time
import threading
from collections import OrderedDict
from typing import Optional, Dict, Tuple


class ProofCache:
    """
    In-memory TTL + size-limited cache for proof blobs, keyed by completion_id.
    Thread-safe via an internal lock. Timestamps refresh on each put.
    """

    def __init__(self, ttl_seconds: int = 600, max_size_mb: int = 500):
        self.ttl = ttl_seconds
        self.max_bytes = max(1, max_size_mb) * 1024 * 1024
        self._lock = threading.RLock()
        self._store: "OrderedDict[str, Tuple[float, bytes]]" = OrderedDict()
        self._size_bytes = 0

    def _evict_expired(self, now: float) -> None:
        to_delete = []
        for key, (ts, data) in list(self._store.items()):
            if now - ts > self.ttl:
                to_delete.append(key)
        for key in to_delete:
            _, data = self._store.pop(key, (0.0, b""))
            self._size_bytes -= len(data)

    def _evict_lru_until_within_budget(self) -> None:
        while self._size_bytes > self.max_bytes and self._store:
            # popitem(last=False) pops LRU
            key, (_, data) = self._store.popitem(last=False)
            self._size_bytes -= len(data)

    def put(self, completion_id: str, blob: bytes) -> None:
        now = time.time()
        with self._lock:
            # Remove existing to reinsert as most-recent
            old = self._store.pop(completion_id, None)
            if old is not None:
                self._size_bytes -= len(old[1])
            self._store[completion_id] = (now, blob)
            self._size_bytes += len(blob)
            # Evict expired then LRU
            self._evict_expired(now)
            self._evict_lru_until_within_budget()

    def get(self, completion_id: str) -> Optional[Tuple[float, bytes, int, int]]:
        """Return (timestamp, blob, size_bytes, ttl_remaining_seconds) or None."""
        now = time.time()
        with self._lock:
            item = self._store.get(completion_id)
            if not item:
                return None
            ts, blob = item
            if now - ts > self.ttl:
                # expired; remove
                self._store.pop(completion_id, None)
                self._size_bytes -= len(blob)
                return None
            # refresh LRU order
            self._store.pop(completion_id)
            self._store[completion_id] = (ts, blob)
            ttl_rem = max(0, int(self.ttl - (now - ts)))
            return ts, blob, len(blob), ttl_rem

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "items": len(self._store),
                "bytes": self._size_bytes,
                "max_bytes": self.max_bytes,
                "ttl_seconds": self.ttl,
            }

