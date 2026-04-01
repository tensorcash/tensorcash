"""
VDF (Verifiable Delay Function) service for proof generation
"""
import threading
import time
import logging

from components.context import LockFreeContext
from components import constants

logger = logging.getLogger(__name__)

# Try to import chiavdf - optional for desktop/standalone mode
try:
    import chiavdf
    CHIAVDF_AVAILABLE = True
except ImportError as e:
    CHIAVDF_AVAILABLE = False
    logger.warning(f"chiavdf not available - VDF proofs disabled: {e}")
LOG_STEP = 1_000_000

class VDFService:
    """Manages the VDF prover with automatic reset on block changes"""
    
    def __init__(self, context: LockFreeContext):
        self.context = context
        self.discriminant_size = constants.VDF_DISCRIMINANT_SIZE
        self.checkpoint_size = constants.VDF_CHECKPOINT_SIZE
        self.update_interval = constants.VDF_UPDATE_INTERVAL
        
        self.prover = None
        self.running = False
        self.thread = None
        self._current_block_hash = None
        self._reset_event = threading.Event()  # Event to trigger immediate reset
        self.next_log_threshold = 0 
        # Back-compat alias attributes for tests
        self._prover = None
        self._running = False
        self._thread = None
        self._current_block = None
        self._discriminant_size = self.discriminant_size
        self._checkpoint_size = self.checkpoint_size
        self._cooldown_paused = False

    def start(self):
        """Start VDF service in background thread"""
        if not CHIAVDF_AVAILABLE:
            logger.warning("VDF service not started - chiavdf not available")
            return

        if self.running:
            logger.warning("VDF service already running")
            return
            
        self.running = True
        self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._thread = self.thread
        self.thread.start()
        logger.info("VDF service started")
    
    def stop(self):
        """Stop VDF service gracefully"""
        logger.info("Stopping VDF service...")
        self.running = False
        self._running = False
        
        # Stop prover first
        if self.prover:
            try:
                self.prover.stop()
            except Exception as e:
                logger.exception(f"Error stopping prover: {e}")
        
        # Then wait for thread
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
            if self.thread.is_alive():
                logger.error("VDF thread did not stop cleanly")
        
        logger.info("VDF service stopped")
    
    def trigger_reset_check(self):
        """Trigger immediate check for block hash change"""
        self._reset_event.set()
    
    def _run(self):
        """Main VDF loop - runs in background thread"""
        logger.info("VDF thread started")
        
        while self.running:
            try:
                # Wait for either timeout or reset event
                reset_triggered = self._reset_event.wait(timeout=self.update_interval)
                if reset_triggered:
                    self._reset_event.clear()
                    logger.debug("VDF reset check triggered by event")
                
                snapshot = self.context.read()

                if self.context.is_mining_paused():
                    if self.prover is not None and not self._cooldown_paused:
                        logger.info(
                            "Mining cooldown active; stopping VDF prover for %.1fs",
                            self.context.get_solution_cooldown_remaining(),
                        )
                        try:
                            self.prover.stop()
                        finally:
                            self.prover = None
                            self._prover = None
                            self._cooldown_paused = True
                    continue
                elif self._cooldown_paused:
                    logger.info("Mining cooldown expired; VDF prover can resume")
                    self._cooldown_paused = False
                
                # Skip VDF when no real mining context (all-zero hash means
                # no ZMQ jobs received yet — running VDF on zeros wastes CPU
                # and leaks memory via chiavdf StreamingProver). EXCEPTION:
                # the genesis block legitimately has an all-zero prev-hash, so
                # the genesis grind must run the VDF over the zero challenge.
                if (snapshot.block_hash and snapshot.block_hash.strip('0') == ''
                        and not constants.GENESIS_GENERATOR):
                    if self.prover is not None:
                        logger.info("Block hash is all zeros — stopping VDF prover (no mining context)")
                        try:
                            self.prover.stop()
                        finally:
                            self.prover = None
                            self._prover = None
                            self._current_block_hash = None
                            self._current_block = None
                    continue

                # Check if we need to reset VDF (retry if prover missing)
                if self.prover is None or snapshot.block_hash != self._current_block_hash:
                    self._reset_prover(snapshot.block_hash)
                
                # Update VDF progress
                if self.prover:
                    self._update_proof()
                
            except Exception as e:
                logger.exception(f"VDF loop error: {e}")
                time.sleep(1.0)  # Back off on error
        
        logger.info("VDF thread stopped")
    
    def _reset_prover(self, block_hash: str):
        """Reset VDF prover with new challenge"""
        logger.info(f"Resetting VDF prover for block {block_hash[:16]}...")
        
        try:
            # Convert hex block hash to bytes for VDF challenge
            # Ensure it's exactly 32 bytes
            if block_hash.startswith('0x'):
                block_hash = block_hash[2:]
            
            # Pad or truncate to exactly 32 bytes
            challenge_hex = block_hash[:64].ljust(64, '0')  # Take first 64 chars (32 bytes), pad if needed
            challenge = bytes.fromhex(challenge_hex)
            
            # Initialize prover on first run or reset existing one
            if self.prover is None:
                # Create new prover with optimal checkpoint size
                # max_iters bounds BOTH how long the prover advances per
                # challenge AND the native checkpoint memory it retains —
                # see constants.VDF_MAX_ITERS.
                self.prover = chiavdf.StreamingProver(
                    challenge,
                    self.discriminant_size,
                    self.checkpoint_size,
                    constants.VDF_MAX_ITERS,
                )
                self.prover.set_verbose(False)
                self.prover.start()
                
                logger.info(f"VDF prover created with discriminant_size={self.discriminant_size}, "
                           f"checkpoint_size={self.checkpoint_size}")
            else:
                # Reset existing prover (much faster than creating new one)
                self.prover.reset(challenge)
                logger.info(f"VDF prover reset with new challenge")
            
            # Only commit current block after successful init/reset.
            self._current_block_hash = block_hash
            self._current_block = block_hash
            self.next_log_threshold = 0 
            
        except Exception as e:
            logger.exception(f"Error resetting VDF prover: {e}")
            self.prover = None
            self._prover = None
    
    def _update_proof(self):
        """Get latest proof from prover and update context"""
        try:
            blob, iterations = self.prover.get_last_available_proof()
            
            if blob and iterations > 0:
                # Convert proof bytes to hex string for JSON compatibility
                proof_hex = blob.hex()
                self.context.update_vdf(proof_hex, iterations)
                
                # Log progress occasionally
                if iterations >= self.next_log_threshold:
                    logger.debug(f"VDF progress: {iterations} iterations, proof: {proof_hex}")
                    self.next_log_threshold += LOG_STEP  # e.g. only every N iterations
                    
        except Exception as e:
            logger.error(f"Error getting VDF proof: {e}")
    
    def get_status(self) -> dict:
        """Get VDF service status"""
        return {
            "available": CHIAVDF_AVAILABLE,
            "running": self.running,
            "has_prover": self.prover is not None,
            "current_block": self._current_block_hash if self._current_block_hash else None,
            "discriminant_size": self.discriminant_size,
            "checkpoint_size": self.checkpoint_size
        }

    # -------- Back-compat helpers for tests --------
    def restart_for_new_block(self, block_hash: str) -> None:
        """Compatibility wrapper to reset prover for a new block immediately."""
        self._reset_event.set()
        self._reset_prover(block_hash)
