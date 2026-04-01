"""Simple tests that can run without full environment setup"""
import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Test what we can without external dependencies
class TestBasicComponents(unittest.TestCase):
    """Test basic component functionality"""
    
    def test_context_import(self):
        """Test that context module can be imported"""
        from components.context import LockFreeContext, MiningSnapshot
        self.assertIsNotNone(LockFreeContext)
        self.assertIsNotNone(MiningSnapshot)
    
    def test_context_basic_functionality(self):
        """Test basic context operations"""
        from components.context import LockFreeContext
        
        context = LockFreeContext("test_hash", "test_target")
        
        # Test initialization
        snapshot = context.read()
        self.assertEqual(snapshot.block_hash, "test_hash")
        self.assertEqual(snapshot.target, "test_target")
        
        # Test update
        context.update_mining("new_hash", "prefix", "new_target", 42)
        new_snapshot = context.read()
        self.assertEqual(new_snapshot.block_hash, "new_hash")
        self.assertEqual(new_snapshot.request_id, 42)
    
    def test_priority_manager_import(self):
        """Test that priority manager can be imported"""
        from components.request_priority_manager import (
            RequestPriorityManager, 
            RequestType, 
            RequestInfo
        )
        self.assertIsNotNone(RequestPriorityManager)
        self.assertIsNotNone(RequestType)
        self.assertIsNotNone(RequestInfo)
    
    def test_priority_manager_basic(self):
        """Test basic priority manager operations"""
        import asyncio
        from components.request_priority_manager import RequestPriorityManager
        
        async def test_async():
            manager = RequestPriorityManager(min_concurrent=2, max_concurrent=4)
            
            # Register external request
            request_id, should_proceed = await manager.register_external_request()
            self.assertTrue(should_proceed)
            self.assertIsNotNone(request_id)
            
            # Get statistics
            stats = manager.get_statistics()
            self.assertEqual(stats["current_external"], 1)
            self.assertEqual(stats["current_dummy"], 0)
            
            # Unregister
            await manager.unregister_request(request_id)
            stats = manager.get_statistics()
            self.assertEqual(stats["current_external"], 0)
        
        # Run async test
        asyncio.run(test_async())
    
    def test_request_type_enum(self):
        """Test RequestType enumeration"""
        from components.request_priority_manager import RequestType
        
        self.assertEqual(RequestType.EXTERNAL.value, "external")
        self.assertEqual(RequestType.DUMMY.value, "dummy")
    
    def test_request_info_dataclass(self):
        """Test RequestInfo dataclass"""
        import time
        from components.request_priority_manager import RequestInfo, RequestType
        
        info = RequestInfo(
            request_id="test-123",
            request_type=RequestType.EXTERNAL,
            start_time=time.time()
        )
        
        self.assertEqual(info.request_id, "test-123")
        self.assertEqual(info.request_type, RequestType.EXTERNAL)
        self.assertTrue(info.age_seconds >= 0)
        self.assertFalse(info.is_stale)  # Should not be stale immediately


class TestPrioritySystemLogic(unittest.TestCase):
    """Test priority system logic without full dependencies"""
    
    def setUp(self):
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
    
    def tearDown(self):
        self.loop.close()
    
    def test_dummy_abortion_for_external(self):
        """Test that dummy requests are aborted for external ones"""
        from components.request_priority_manager import RequestPriorityManager
        
        async def test_scenario():
            manager = RequestPriorityManager(
                min_concurrent=2, 
                max_concurrent=3,
                batch_size=5
            )
            
            # Fill up with dummy requests
            dummy1 = await manager.register_dummy_request("dummy-1")
            dummy2 = await manager.register_dummy_request("dummy-2")
            dummy3 = await manager.register_dummy_request("dummy-3")
            
            stats = manager.get_statistics()
            self.assertEqual(stats["current_dummy"], 3)
            
            # Now add external - should abort a dummy
            ext_id, should_proceed = await manager.register_external_request("ext-1")
            self.assertTrue(should_proceed)
            
            stats = manager.get_statistics()
            self.assertEqual(stats["current_external"], 1)
            self.assertEqual(stats["current_dummy"], 2)  # One dummy was aborted
            self.assertEqual(stats["total_aborted"], 1)
        
        self.loop.run_until_complete(test_scenario())
    
    def test_capacity_limits(self):
        """Test that capacity limits are enforced"""
        from components.request_priority_manager import RequestPriorityManager
        
        async def test_scenario():
            manager = RequestPriorityManager(
                min_concurrent=1,
                max_concurrent=2
            )
            
            # Add two external requests (at max)
            ext1, proceed1 = await manager.register_external_request()
            ext2, proceed2 = await manager.register_external_request()
            
            self.assertTrue(proceed1)
            self.assertTrue(proceed2)
            
            # Third should fail (no dummies to abort)
            ext3, proceed3 = await manager.register_external_request()
            self.assertFalse(proceed3)  # Should be rejected
            
            stats = manager.get_statistics()
            self.assertEqual(stats["current_external"], 2)
            self.assertFalse(stats["can_accept_external"])
        
        self.loop.run_until_complete(test_scenario())
    
    def test_minimum_concurrency_protection(self):
        """Test that minimum concurrency is maintained"""
        from components.request_priority_manager import RequestPriorityManager
        
        async def test_scenario():
            manager = RequestPriorityManager(
                min_concurrent=3,
                max_concurrent=5
            )
            
            # Add minimum dummies
            for i in range(3):
                await manager.register_dummy_request(f"dummy-{i}")
            
            # Should generate dummy when below minimum
            should_gen = await manager.should_generate_dummy()
            self.assertFalse(should_gen)  # We're at minimum
            
            # Remove one
            await manager.unregister_request("dummy-0")
            
            # Now should need to generate
            should_gen = await manager.should_generate_dummy()
            self.assertTrue(should_gen)
        
        self.loop.run_until_complete(test_scenario())


if __name__ == '__main__':
    unittest.main()