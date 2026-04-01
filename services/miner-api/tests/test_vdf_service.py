"""Unit tests for VDF Service (using chiavdf StreamingProver stub)"""
import unittest
import threading
import time
from unittest.mock import Mock
import sys
import os
import types

# Put src on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Install a lightweight chiavdf stub before importing VDFService
if "chiavdf" not in sys.modules:
    chiavdf_stub = types.ModuleType("chiavdf")

    class _StubProver:
        def __init__(self, challenge, disc_size, checkpoint_size, _cap):
            self.challenge = challenge
            self.disc_size = disc_size
            self.checkpoint_size = checkpoint_size
            self.iterations = 0
            self.verbose = False
            self.started = False
        def set_verbose(self, v: bool):
            self.verbose = v
        def start(self):
            self.started = True
        def reset(self, challenge):
            self.challenge = challenge
            self.iterations = 0
        def stop(self):
            self.started = False
        def get_last_available_proof(self):
            # Produce a monotonically increasing iteration count
            self.iterations += 10000
            return (b"proof-bytes", self.iterations)

    chiavdf_stub.StreamingProver = _StubProver
    sys.modules["chiavdf"] = chiavdf_stub

from components.vdf_service import VDFService
from components.context import LockFreeContext


class TestVDFService(unittest.TestCase):
    """Test cases for VDF proof generation service"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.vdf_service = VDFService(self.context)
    
    def tearDown(self):
        """Clean up after tests"""
        if self.vdf_service._running:
            self.vdf_service.stop()
    
    def test_initialization(self):
        """Test VDF service initialization"""
        self.assertIsNotNone(self.vdf_service)
        self.assertFalse(self.vdf_service._running)
        self.assertIsNone(self.vdf_service._thread)
        self.assertIsNone(self.vdf_service._prover)
        self.assertEqual(self.vdf_service._current_block, None)
    
    def test_start_stop(self):
        """Test starting and stopping the service"""
        # Start service
        self.vdf_service.start()
        self.assertTrue(self.vdf_service._running)
        self.assertIsNotNone(self.vdf_service._thread)
        self.assertTrue(self.vdf_service._thread.is_alive())
        
        # Stop service
        self.vdf_service.stop()
        self.assertFalse(self.vdf_service._running)
        
        # Thread should stop within reasonable time
        if self.vdf_service._thread:
            self.vdf_service._thread.join(timeout=2)
            self.assertFalse(self.vdf_service._thread.is_alive())
    
    def test_restart_on_block_change(self):
        """Test VDF restart when block changes"""
        # Update context with initial block (use valid hex)
        self.context.update_mining("abcd" * 16, "prefix", "target", 1)
        
        # Start service
        self.vdf_service.start()
        time.sleep(0.2)  # Let it initialize
        
        # Verify prover was created for first block
        self.assertEqual(self.vdf_service._current_block, "abcd" * 16)
        
        # Change block (use valid hex)
        self.context.update_mining("1234" * 16, "prefix", "target", 2)
        
        # Trigger restart
        self.vdf_service.restart_for_new_block("1234" * 16)
        time.sleep(0.2)  # Let it restart
        
        # Verify prover was recreated for second block
        self.assertEqual(self.vdf_service._current_block, "1234" * 16)
        
        self.vdf_service.stop()
    
    def test_proof_generation(self):
        """Test VDF proof generation and context update"""
        # Update context with block (use valid hex)
        self.context.update_mining("deadbeef" * 8, "prefix", "target", 1)
        
        # Start service
        self.vdf_service.start()
        
        # Wait for some proofs to be generated
        time.sleep(0.5)
        
        # Check that VDF proof was updated in context
        snapshot = self.context.read()
        self.assertIsNotNone(snapshot.vdf_proof)
        self.assertGreater(snapshot.vdf_tick, 0)
        
        self.vdf_service.stop()
    
    def test_get_status(self):
        """Test status reporting"""
        # Initial status
        status = self.vdf_service.get_status()
        self.assertFalse(status["running"])
        self.assertFalse(status["has_prover"])
        self.assertIsNone(status["current_block"])
        self.assertEqual(status["discriminant_size"], self.vdf_service.discriminant_size)
        self.assertEqual(status["checkpoint_size"], self.vdf_service.checkpoint_size)
        
        # Start service and update status (use valid hex)
        self.context.update_mining("cafebabe" * 8, "prefix", "target", 1)
        self.vdf_service.start()
        time.sleep(0.2)
        
        status = self.vdf_service.get_status()
        self.assertTrue(status["running"])
        self.assertTrue(status["has_prover"])
        self.assertEqual(status["current_block"], "cafebabe" * 8)
        
        self.vdf_service.stop()
    
    def test_error_handling(self):
        """Test error handling in VDF computation"""
        # Update context and start service (use valid hex)
        self.context.update_mining("badc0de" * 9 + "a", "prefix", "target", 1)
        self.vdf_service.start()
        
        # Let it try to compute (should handle error gracefully)
        time.sleep(0.3)
        
        # Service should still be running despite errors
        self.assertTrue(self.vdf_service._running)
        
        # Context should not have invalid proof
        snapshot = self.context.read()
        # Proof might be None or the last valid one
        
        self.vdf_service.stop()
    
    def test_concurrent_restart_safety(self):
        """Test thread safety of concurrent restarts"""
        self.context.update_mining("block1", "prefix", "target", 1)
        self.vdf_service.start()
        
        errors = []
        
        def restart_thread(block_num):
            try:
                for i in range(10):
                    block_hash = f"block_{block_num}_{i}"
                    self.vdf_service.restart_for_new_block(block_hash)
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)
        
        # Create multiple threads trying to restart
        threads = []
        for i in range(3):
            t = threading.Thread(target=restart_thread, args=(i,))
            threads.append(t)
            t.start()
        
        # Wait for threads
        for t in threads:
            t.join(timeout=5)
        
        # Should handle concurrent restarts without errors
        self.assertEqual(len(errors), 0)
        
        # Service should still be running
        self.assertTrue(self.vdf_service._running)
        
        self.vdf_service.stop()


class TestVDFIntegration(unittest.TestCase):
    """Integration-style test relying on chiavdf stub"""
    def setUp(self):
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.vdf_service = VDFService(self.context)

    def test_vdf_context_synchronization(self):
        # Update context and start VDF (use valid hex)
        self.context.update_mining("feedface" * 8, "prefix", "target", 1)
        self.vdf_service.start()
        time.sleep(0.5)

        # Verify context has VDF proof and tick advanced
        snapshot = self.context.read()
        self.assertIsNotNone(snapshot.vdf_proof)
        self.assertTrue(self.context.vdf_initialised)

        # Stop and verify last proof remains
        self.vdf_service.stop()
        final_snapshot = self.context.read()
        self.assertIsNotNone(final_snapshot.vdf_proof)


if __name__ == '__main__':
    unittest.main()
