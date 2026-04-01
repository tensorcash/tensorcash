"""Comprehensive unit tests for ProofCache"""
import unittest
import sys
import os
import time
import threading
from unittest.mock import patch

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from components.proof_cache import ProofCache


class TestProofCache(unittest.TestCase):
    def setUp(self):
        """Set up test cache"""
        self.cache = ProofCache(ttl_seconds=10, max_size_mb=1)

    def test_initialization(self):
        """Test ProofCache initialization"""
        cache = ProofCache(ttl_seconds=300, max_size_mb=100)
        self.assertEqual(cache.ttl, 300)
        self.assertEqual(cache.max_bytes, 100 * 1024 * 1024)
        self.assertEqual(len(cache._store), 0)
        self.assertEqual(cache._size_bytes, 0)

    def test_initialization_minimum_size(self):
        """Test ProofCache initialization with minimum size"""
        cache = ProofCache(ttl_seconds=300, max_size_mb=0)
        self.assertEqual(cache.max_bytes, 1024 * 1024)  # Should be at least 1MB

    def test_put_and_get_basic(self):
        """Test basic put and get operations"""
        test_data = b"test_data_123"
        self.cache.put("test_id", test_data)
        
        result = self.cache.get("test_id")
        self.assertIsNotNone(result)
        
        timestamp, blob, size, ttl_remaining = result
        self.assertEqual(blob, test_data)
        self.assertEqual(size, len(test_data))
        self.assertLessEqual(ttl_remaining, 10)
        self.assertGreaterEqual(ttl_remaining, 0)
        self.assertIsInstance(timestamp, float)

    def test_get_nonexistent_key(self):
        """Test getting a non-existent key"""
        result = self.cache.get("nonexistent")
        self.assertIsNone(result)

    def test_put_overwrites_existing(self):
        """Test that putting with same key overwrites"""
        self.cache.put("key1", b"data1")
        self.cache.put("key1", b"data2")
        
        result = self.cache.get("key1")
        self.assertIsNotNone(result)
        _, blob, _, _ = result
        self.assertEqual(blob, b"data2")

    def test_ttl_expiration(self):
        """Test TTL expiration"""
        cache = ProofCache(ttl_seconds=1, max_size_mb=1)
        cache.put("expire_test", b"data")
        
        # Should exist immediately
        result = cache.get("expire_test")
        self.assertIsNotNone(result)
        
        # Wait for expiration
        time.sleep(1.2)
        
        # Should be expired
        result = cache.get("expire_test")
        self.assertIsNone(result)

    def test_lru_eviction(self):
        """Test LRU eviction based on size limit"""
        cache = ProofCache(ttl_seconds=100, max_size_mb=1)  # 1MB limit
        
        # Add data that exceeds size limit
        cache.put("item1", b"x" * (400 * 1024))  # 400KB
        cache.put("item2", b"y" * (400 * 1024))  # 400KB
        cache.put("item3", b"z" * (400 * 1024))  # 400KB - should trigger eviction
        
        # item1 should be evicted (LRU)
        self.assertIsNone(cache.get("item1"))
        self.assertIsNotNone(cache.get("item2"))
        self.assertIsNotNone(cache.get("item3"))

    def test_lru_order_refresh_on_get(self):
        """Test that get refreshes LRU order"""
        cache = ProofCache(ttl_seconds=100, max_size_mb=1)
        
        cache.put("item1", b"x" * (400 * 1024))
        cache.put("item2", b"y" * (400 * 1024))
        
        # Access item1 to refresh its LRU position
        cache.get("item1")
        
        # Add item3 to trigger eviction
        cache.put("item3", b"z" * (400 * 1024))
        
        # item2 should be evicted (was LRU), item1 should remain
        self.assertIsNotNone(cache.get("item1"))
        self.assertIsNone(cache.get("item2"))
        self.assertIsNotNone(cache.get("item3"))

    def test_lru_order_refresh_on_put(self):
        """Test that put refreshes LRU order for existing keys"""
        cache = ProofCache(ttl_seconds=100, max_size_mb=1)
        
        cache.put("item1", b"x" * (400 * 1024))
        cache.put("item2", b"y" * (400 * 1024))
        
        # Update item1 to refresh its position (with smaller data)
        cache.put("item1", b"updated" * 50000)  # ~350KB
        
        # Add item3 to trigger eviction
        cache.put("item3", b"z" * (400 * 1024))
        
        # item2 should be evicted, item1 should remain
        self.assertIsNotNone(cache.get("item1"))
        self.assertIsNone(cache.get("item2"))
        self.assertIsNotNone(cache.get("item3"))

    def test_expired_items_eviction(self):
        """Test automatic eviction of expired items during put operations"""
        cache = ProofCache(ttl_seconds=1, max_size_mb=10)
        
        # Add items that will expire
        cache.put("expire1", b"data1")
        cache.put("expire2", b"data2")
        
        # Wait for expiration
        time.sleep(1.2)
        
        # Add new item - should trigger expired item cleanup
        cache.put("new", b"new_data")
        
        # Check internal state - expired items should be cleaned up
        self.assertEqual(len(cache._store), 1)
        self.assertIn("new", cache._store)

    def test_stats(self):
        """Test stats reporting"""
        # Empty cache
        stats = self.cache.stats()
        self.assertEqual(stats["items"], 0)
        self.assertEqual(stats["bytes"], 0)
        self.assertEqual(stats["max_bytes"], 1024 * 1024)
        self.assertEqual(stats["ttl_seconds"], 10)
        
        # Add some data
        self.cache.put("item1", b"data1")
        self.cache.put("item2", b"data22")
        
        stats = self.cache.stats()
        self.assertEqual(stats["items"], 2)
        self.assertEqual(stats["bytes"], 5 + 6)  # len("data1") + len("data22")

    def test_size_tracking_accuracy(self):
        """Test that internal size tracking is accurate"""
        data1 = b"x" * 1000
        data2 = b"y" * 2000
        
        self.cache.put("item1", data1)
        self.assertEqual(self.cache._size_bytes, 1000)
        
        self.cache.put("item2", data2)
        self.assertEqual(self.cache._size_bytes, 3000)
        
        # Overwrite item1
        data3 = b"z" * 500
        self.cache.put("item1", data3)
        self.assertEqual(self.cache._size_bytes, 2500)  # 500 + 2000

    def test_thread_safety(self):
        """Test thread safety of cache operations"""
        cache = ProofCache(ttl_seconds=60, max_size_mb=10)
        results = []
        errors = []
        
        def writer_thread(thread_id):
            try:
                for i in range(100):
                    key = f"key_{thread_id}_{i}"
                    data = f"data_{thread_id}_{i}".encode()
                    cache.put(key, data)
                results.append(f"writer_{thread_id}_done")
            except Exception as e:
                errors.append(e)
        
        def reader_thread(thread_id):
            try:
                for i in range(200):
                    key = f"key_{thread_id % 3}_{i % 50}"  # Read from writer threads
                    result = cache.get(key)
                    # Result may or may not exist depending on timing
                results.append(f"reader_{thread_id}_done")
            except Exception as e:
                errors.append(e)
        
        # Start threads
        threads = []
        for i in range(3):
            threads.append(threading.Thread(target=writer_thread, args=(i,)))
            threads.append(threading.Thread(target=reader_thread, args=(i,)))
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join(timeout=5)
        
        # Should complete without errors
        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(results), 6)  # 3 writers + 3 readers

    def test_edge_case_empty_data(self):
        """Test handling of empty data"""
        self.cache.put("empty", b"")
        
        result = self.cache.get("empty")
        self.assertIsNotNone(result)
        _, blob, size, _ = result
        self.assertEqual(blob, b"")
        self.assertEqual(size, 0)

    def test_edge_case_very_large_single_item(self):
        """Test handling of single item larger than cache"""
        cache = ProofCache(ttl_seconds=60, max_size_mb=1)
        
        # Item larger than cache capacity - it will be added then immediately evicted
        large_data = b"x" * (2 * 1024 * 1024)  # 2MB
        cache.put("oversized", large_data)
        
        # The oversized item gets evicted immediately after insertion
        result = cache.get("oversized")
        self.assertIsNone(result)  # Should be evicted
        
        # Small item should fit
        cache.put("small", b"small_data")
        self.assertIsNotNone(cache.get("small"))

    @patch('time.time')
    def test_time_mocking_for_ttl(self, mock_time):
        """Test TTL behavior with mocked time"""
        mock_time.return_value = 1000.0
        
        cache = ProofCache(ttl_seconds=10, max_size_mb=1)
        cache.put("test", b"data")
        
        # Advance time by 5 seconds
        mock_time.return_value = 1005.0
        result = cache.get("test")
        self.assertIsNotNone(result)
        _, _, _, ttl = result
        self.assertEqual(ttl, 5)
        
        # Advance time by 11 seconds total
        mock_time.return_value = 1011.0
        result = cache.get("test")
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()

