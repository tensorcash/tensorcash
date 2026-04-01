"""Unit tests for LockFreeContext"""
import unittest
import time
import threading
from unittest.mock import patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from components.context import LockFreeContext, MiningSnapshot


class TestLockFreeContext(unittest.TestCase):
    """Test cases for lock-free context management"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.default_block_hash = "0" * 64
        self.default_target = "ffff" * 16
        self.context = LockFreeContext(self.default_block_hash, self.default_target)
    
    def test_initialization(self):
        """Test context initialization"""
        snapshot = self.context.read()
        self.assertEqual(snapshot.block_hash, self.default_block_hash)
        self.assertEqual(snapshot.target, self.default_target)
        self.assertEqual(snapshot.header_prefix, "0" * 152)
        self.assertEqual(snapshot.request_id, 0)
        self.assertIsNone(snapshot.vdf_proof)
        self.assertEqual(snapshot.vdf_tick, 0)
        self.assertFalse(self.context.miner_initialised)
        self.assertFalse(self.context.vdf_initialised)
    
    def test_update_mining(self):
        """Test mining parameter updates"""
        new_hash = "1" * 64
        new_prefix = "2" * 152
        new_target = "3" * 64
        new_request_id = 42
        
        # Update should return True for block change
        block_changed = self.context.update_mining(
            new_hash, new_prefix, new_target, new_request_id
        )
        self.assertTrue(block_changed)
        self.assertTrue(self.context.miner_initialised)
        
        # Verify snapshot updated
        snapshot = self.context.read()
        self.assertEqual(snapshot.block_hash, new_hash)
        self.assertEqual(snapshot.header_prefix, new_prefix)
        self.assertEqual(snapshot.target, new_target)
        self.assertEqual(snapshot.request_id, new_request_id)
        
        # Update with same block should return False
        block_changed = self.context.update_mining(
            new_hash, new_prefix, new_target, new_request_id + 1
        )
        self.assertFalse(block_changed)
    
    def test_update_vdf(self):
        """Test VDF proof updates"""
        vdf_proof = "base64encodedproof"
        vdf_tick = 1000000
        
        self.context.update_vdf(vdf_proof, vdf_tick)
        self.assertTrue(self.context.vdf_initialised)
        
        snapshot = self.context.read()
        self.assertEqual(snapshot.vdf_proof, vdf_proof)
        self.assertEqual(snapshot.vdf_tick, vdf_tick)
    
    def test_vdf_update_overwrites(self):
        """Test VDF updates overwrite previous values"""
        # Set VDF first
        self.context.update_vdf("proof1", 1000)
        snapshot1 = self.context.read()
        self.assertEqual(snapshot1.vdf_proof, "proof1")
        self.assertEqual(snapshot1.vdf_tick, 1000)
        
        # Update VDF with new values
        self.context.update_vdf("proof2", 2000)
        snapshot2 = self.context.read()
        self.assertEqual(snapshot2.vdf_proof, "proof2")
        self.assertEqual(snapshot2.vdf_tick, 2000)
    
    def test_snapshot_immutability(self):
        """Test that snapshots are immutable"""
        snapshot1 = self.context.read()
        self.context.update_mining("new_hash", "new_prefix", "new_target", 1)
        snapshot2 = self.context.read()
        
        # Original snapshot should be unchanged
        self.assertEqual(snapshot1.block_hash, self.default_block_hash)
        self.assertEqual(snapshot2.block_hash, "new_hash")
    
    def test_get_status(self):
        """Test status reporting"""
        # Initial status
        status = self.context.get_status()
        # Block hash is shortened to 16 chars + "..." in status
        self.assertTrue(status["block_hash"].startswith("0000000000000000"))
        self.assertTrue(status["block_hash"].endswith("..."))
        self.assertEqual(status["request_id"], 0)
        self.assertEqual(status["vdf_tick"], 0)
        self.assertFalse(status["has_vdf_proof"])
        self.assertIn("age_seconds", status)
        
        # Update and check status
        self.context.update_mining("a" * 64, "prefix", "target", 42)
        self.context.update_vdf("proof", 1000)
        
        status = self.context.get_status()
        self.assertTrue(status["block_hash"].startswith("aaaaaaaaaaaaaaaa"))
        self.assertTrue(status["block_hash"].endswith("..."))
        self.assertEqual(status["request_id"], 42)
        self.assertEqual(status["vdf_tick"], 1000)
        self.assertTrue(status["has_vdf_proof"])
    
    def test_concurrent_updates(self):
        """Test thread safety of concurrent updates"""
        results = []
        errors = []
        
        def update_mining_thread(thread_id):
            try:
                for i in range(100):
                    block_hash = f"{thread_id}_{i}_" + "0" * 50
                    self.context.update_mining(block_hash, "prefix", "target", i)
                results.append(f"mining_{thread_id}_complete")
            except Exception as e:
                errors.append(e)
        
        def update_vdf_thread(thread_id):
            try:
                for i in range(100):
                    self.context.update_vdf(f"proof_{thread_id}_{i}", i * 1000)
                results.append(f"vdf_{thread_id}_complete")
            except Exception as e:
                errors.append(e)
        
        def read_thread(thread_id):
            try:
                for i in range(200):
                    snapshot = self.context.read()
                    # Just verify we get a valid snapshot
                    self.assertIsInstance(snapshot, MiningSnapshot)
                results.append(f"read_{thread_id}_complete")
            except Exception as e:
                errors.append(e)
        
        # Create threads
        threads = []
        for i in range(3):
            threads.append(threading.Thread(target=update_mining_thread, args=(i,)))
            threads.append(threading.Thread(target=update_vdf_thread, args=(i,)))
            threads.append(threading.Thread(target=read_thread, args=(i,)))
        
        # Start all threads
        for t in threads:
            t.start()
        
        # Wait for completion
        for t in threads:
            t.join(timeout=5)
        
        # Check results
        self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
        self.assertEqual(len(results), 9)  # 3 mining + 3 vdf + 3 read threads
        
        # Final snapshot should be valid
        final_snapshot = self.context.read()
        self.assertIsInstance(final_snapshot, MiningSnapshot)
        self.assertIsNotNone(final_snapshot.block_hash)
        self.assertIsNotNone(final_snapshot.vdf_proof)


class TestMiningSnapshot(unittest.TestCase):
    """Test cases for MiningSnapshot dataclass"""
    
    def test_snapshot_creation(self):
        """Test creating a mining snapshot"""
        snapshot = MiningSnapshot(
            block_hash="hash",
            header_prefix="prefix",
            target="target",
            request_id=42,
            vdf_proof="proof",
            vdf_tick=1000,
            timestamp=time.time()
        )
        
        self.assertEqual(snapshot.block_hash, "hash")
        self.assertEqual(snapshot.header_prefix, "prefix")
        self.assertEqual(snapshot.target, "target")
        self.assertEqual(snapshot.request_id, 42)
        self.assertEqual(snapshot.vdf_proof, "proof")
        self.assertEqual(snapshot.vdf_tick, 1000)
    
    def test_snapshot_immutability(self):
        """Test that snapshots are frozen/immutable"""
        snapshot = MiningSnapshot(
            block_hash="hash",
            header_prefix="prefix",
            target="target",
            request_id=42,
            vdf_proof="proof",
            vdf_tick=1000,
            timestamp=time.time()
        )
        
        # Attempting to modify should raise an error
        with self.assertRaises(AttributeError):
            snapshot.block_hash = "new_hash"


if __name__ == '__main__':
    unittest.main()