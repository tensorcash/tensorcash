"""
Lock-free context management for mining state
"""
import time
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class MiningSnapshot:
    """Immutable snapshot of mining context"""
    block_hash: str
    header_prefix: str
    target: str
    request_id: int
    vdf_proof: Optional[str]
    vdf_tick: int
    timestamp: float
    # Slice 11 dual-threshold emission: the broker's UNADJUSTED base
    # share target from the MINE_REQUEST template. Carried verbatim —
    # the proxy derives the model-adjusted threshold per request at
    # injection time (the adjustment depends on the selected model's
    # difficulty, which is not known here). None on the legacy zmq
    # core-node path: share emission is broker-mode only.
    base_share_target: Optional[str] = None

class LockFreeContext:
    """
    Thread-safe context using atomic reference swapping.
    
    This relies on Python's GIL making reference assignment atomic.
    No additional locking needed for the simple read/write pattern.
    """
    
    def __init__(self, default_block_hash: str, target: str):
        self._snapshot = MiningSnapshot(
            block_hash=default_block_hash,
            header_prefix="0" * 152,
            target=target,
            request_id=0,
            vdf_proof=None,
            vdf_tick=0,
            timestamp=time.time()
        )
        logger.info(f"Context initialized with block hash: {default_block_hash[:16]}...")
        self.miner_initialised = False
        self.vdf_initialised = False
        self._solution_cooldown_until = 0.0
        self._expected_model_identifier = ""
    
    def update_mining(self, block_hash: str, header_prefix: str,
                     target: str, request_id: int,
                     base_share_target: Optional[str] = None) -> bool:
        """
        Update mining parameters. Returns True if block_hash changed.

        Thread-safe: Creates new immutable object, then atomically assigns reference.
        """
        old = self._snapshot
        block_changed = block_hash != old.block_hash
        # Reset VDF state on new block to avoid mixing proofs across challenges.
        vdf_proof = None if block_changed else old.vdf_proof
        vdf_tick = 0 if block_changed else old.vdf_tick
        if block_changed:
            self.vdf_initialised = False
        self._snapshot = MiningSnapshot(
            block_hash=block_hash,
            header_prefix=header_prefix,
            target=target,
            request_id=request_id,
            vdf_proof=vdf_proof,
            vdf_tick=vdf_tick,
            timestamp=time.time(),
            # Always taken from THIS job, never carried over: a job
            # without a share target (zmq path) must not inherit a
            # stale one from a previous broker job.
            base_share_target=base_share_target,
        )

        if block_changed:
            logger.info(f"Block hash changed: {old.block_hash[:16]}... -> {block_hash[:16]}...")
        
        if not self.miner_initialised:
            self.miner_initialised = True
        return block_changed
    
    def update_vdf(self, vdf_proof: str, vdf_tick: int):
        """
        Update VDF proof and tick.
        
        Thread-safe: Creates new immutable object, then atomically assigns reference.
        """
        old = self._snapshot
        self._snapshot = MiningSnapshot(
            block_hash=old.block_hash,
            header_prefix=old.header_prefix,
            target=old.target,
            request_id=old.request_id,
            vdf_proof=vdf_proof,
            vdf_tick=vdf_tick,
            timestamp=old.timestamp,
            base_share_target=old.base_share_target,
        )
        if not self.vdf_initialised:
            self.vdf_initialised = True

        logger.debug(f"VDF updated: tick={vdf_tick}")

    def activate_solution_cooldown(self, duration_seconds: float, reason: str = "solution_found") -> float:
        """
        Pause mining for the given duration starting now.

        Returns the UNIX timestamp when the cooldown expires.
        """
        now = time.time()
        duration_seconds = max(0.0, float(duration_seconds))
        expires_at = now + duration_seconds
        old_expires_at = self._solution_cooldown_until
        self._solution_cooldown_until = max(old_expires_at, expires_at)
        remaining = max(0.0, self._solution_cooldown_until - now)
        logger.info(
            "Mining cooldown activated for %.1fs (reason=%s, until=%.3f)",
            remaining,
            reason,
            self._solution_cooldown_until,
        )
        return self._solution_cooldown_until

    def get_solution_cooldown_remaining(self) -> float:
        """Return remaining mining cooldown in seconds."""
        return max(0.0, self._solution_cooldown_until - time.time())

    def is_mining_paused(self) -> bool:
        """Return True while solution cooldown is active."""
        return self.get_solution_cooldown_remaining() > 0.0

    def set_expected_model_identifier(self, model_name: str, model_commit: str) -> None:
        """
        Set expected runtime model identifier for proof filtering.
        Empty name+commit clears the expectation (auto-select mode).
        """
        model_name = (model_name or "").strip()
        model_commit = (model_commit or "").strip()
        if bool(model_name) != bool(model_commit):
            return
        self._expected_model_identifier = (
            f"{model_name}@{model_commit}" if model_name and model_commit else ""
        )

    def get_expected_model_identifier(self) -> str:
        """Get expected runtime model identifier used for proof filtering."""
        return self._expected_model_identifier
    
    def read(self) -> MiningSnapshot:
        """
        Get current snapshot atomically.
        
        Thread-safe: Reference read is atomic under GIL.
        """
        return self._snapshot
    
    def get_status(self) -> dict:
        """Get status information for monitoring"""
        snapshot = self._snapshot
        cooldown_remaining = self.get_solution_cooldown_remaining()
        return {
            "block_hash": snapshot.block_hash[:16] + "..." if snapshot.block_hash else "none",
            "request_id": snapshot.request_id,
            "vdf_tick": snapshot.vdf_tick,
            "has_vdf_proof": snapshot.vdf_proof is not None,
            "age_seconds": round(time.time() - snapshot.timestamp, 1),
            "mining_paused": cooldown_remaining > 0.0,
            "cooldown_remaining_seconds": round(cooldown_remaining, 1),
            "expected_model_identifier": self._expected_model_identifier,
        }
