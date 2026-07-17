"""Comprehensive unit tests for PriorityRequestManager"""
import sys
import os
import types

# Add src to path FIRST
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Mock the utils module BEFORE any other imports
if "utils.uint256_arithmetics" not in sys.modules:
    utils_pkg = types.ModuleType("utils")
    uint256_mod = types.ModuleType("utils.uint256_arithmetics")
    def set_compact(x):
        return x
    def get_compact(x):
        return x
    def adjust_nbits_by_multiplier(bits, mult, default=None):
        return {"target_bytes": b"\xff" * 32, "nbits": 0x1d00ffff}
    uint256_mod.set_compact = set_compact
    uint256_mod.get_compact = get_compact
    uint256_mod.adjust_nbits_by_multiplier = adjust_nbits_by_multiplier
    sys.modules["utils"] = utils_pkg
    sys.modules["utils.uint256_arithmetics"] = uint256_mod

# Now import the rest AFTER mocking
import unittest
import asyncio
import time
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from components.proxy_with_priority import PriorityRequestManager
from components.context import LockFreeContext
from components.request_priority_manager import RequestPriorityManager
from components.constants import ModelConfig


class TestPriorityRequestManager(AioHTTPTestCase):
    """Test cases for PriorityRequestManager"""
    
    async def get_application(self):
        """Create test application"""
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.manager = PriorityRequestManager(self.context)
        
        # Mock dependencies
        self.manager.model_client = Mock()
        self.manager.model_client._initialized = True
        self.manager.model_client.models_by_name = {
            "test-model": [ModelConfig(
                model_hash="hash123",
                model_name="test-model",
                model_commit="commit123",
                difficulty=1000000,
                ipfs_cid="Qm123"
            )]
        }
        
        # Create app
        app = web.Application()
        app.router.add_post('/v1/completions', self.manager.proxy_request)
        app.router.add_post('/v1/responses', self.manager.proxy_request)
        
        return app
    
    async def setUpAsync(self):
        """Async setup"""
        await super().setUpAsync()
        
        # Mock session
        self.manager.session = AsyncMock()
        self.manager.active_requests = {}
    
    def test_initialization(self):
        """Test PriorityRequestManager initialization"""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        manager = PriorityRequestManager(context)
        
        self.assertIsInstance(manager.priority_manager, RequestPriorityManager)
        self.assertEqual(manager.priority_manager.min_concurrent, manager.min_active)
        self.assertEqual(manager.priority_manager.max_concurrent, manager.min_active * 2)
        self.assertIsInstance(manager._batch_tasks, dict)
        self.assertEqual(len(manager._batch_tasks), 0)
    
    @unittest_run_loop
    async def test_start(self):
        """Test starting the priority manager"""
        with patch.object(self.manager, '_monitor_loop', new_callable=AsyncMock) as mock_monitor:
            with patch('asyncio.create_task') as mock_create_task:
                await self.manager.start()
                
                # Should create task for priority cleanup
                mock_create_task.assert_called()
    
    @unittest_run_loop
    async def test_handle_completion_request_with_capacity(self):
        """Test handling completion request when capacity is available"""
        # Mock priority manager to allow request
        self.manager.priority_manager.register_external_request = AsyncMock(
            return_value=("ext-123", True)
        )
        self.manager.priority_manager.unregister_request = AsyncMock()
        
        # Mock the parent class method
        with patch.object(super(PriorityRequestManager, self.manager), 
                         '_handle_completion_request', 
                         new_callable=AsyncMock) as mock_parent:
            mock_parent.return_value = web.Response(text="success")
            
            request = Mock(spec=web.Request)
            response = await self.manager._handle_completion_request(request)
            
            # Should proceed with request
            self.assertEqual(response.text, "success")
            self.manager.priority_manager.register_external_request.assert_called_once()
            self.manager.priority_manager.unregister_request.assert_called_once_with("ext-123")
            self.assertNotIn("ext-123", self.manager.active_requests)
    
    @unittest_run_loop
    async def test_handle_completion_request_no_capacity(self):
        """Test handling completion request when at capacity"""
        # Mock priority manager to deny request
        self.manager.priority_manager.register_external_request = AsyncMock(
            return_value=("ext-123", False)
        )
        
        request = Mock(spec=web.Request)
        response = await self.manager._handle_completion_request(request)
        
        # Should return 503
        self.assertEqual(response.status, 503)
        self.assertIn("capacity", response.text)
    
    @unittest_run_loop
    async def test_handle_responses_request_with_capacity(self):
        """Test handling responses request when capacity is available"""
        # Mock priority manager to allow request
        self.manager.priority_manager.register_external_request = AsyncMock(
            return_value=("ext-456", True)
        )
        self.manager.priority_manager.unregister_request = AsyncMock()
        
        # Mock the parent class method
        with patch.object(super(PriorityRequestManager, self.manager),
                         '_handle_responses_request',
                         new_callable=AsyncMock) as mock_parent:
            mock_parent.return_value = web.Response(text="response_success")
            
            request = Mock(spec=web.Request)
            response = await self.manager._handle_responses_request(request)
            
            # Should proceed with request
            self.assertEqual(response.text, "response_success")
            self.manager.priority_manager.register_external_request.assert_called_once()
            self.manager.priority_manager.unregister_request.assert_called_once_with("ext-456")
    
    @unittest_run_loop
    async def test_generate_dummy_batch(self):
        """Test batch generation of dummy requests"""
        # Mock priority manager
        self.manager.priority_manager.should_generate_dummy = AsyncMock(
            side_effect=[True, True, False]  # Generate 2, then stop
        )
        
        # Mock single dummy generation
        self.manager._generate_single_dummy = AsyncMock()
        
        await self.manager._generate_dummy_batch()
        
        # Should have generated 2 dummy requests
        self.assertEqual(self.manager._generate_single_dummy.call_count, 2)
        
        # Batch tasks should be cleaned up
        self.assertEqual(len(self.manager._batch_tasks), 0)
    
    @unittest_run_loop
    async def test_generate_single_dummy_success(self):
        """Test successful single dummy generation"""
        # Setup mocks
        self.manager.priority_manager.register_dummy_request = AsyncMock(
            return_value="dummy-123"
        )
        self.manager.priority_manager.unregister_request = AsyncMock()
        self.manager.priority_manager.attach_task = AsyncMock()
        
        self.manager.model_client = Mock()
        self.manager.model_client.models_by_name = {"Qwen/Qwen3-8B": [{}]}
        
        self.manager.prompt_generator = Mock()
        self.manager.prompt_generator.generate_prompt = Mock(return_value="test prompt")
        
        self.manager._inject_pow_data = Mock(return_value={"injected": "data"})
        self.manager._execute_dummy_request = AsyncMock()
        
        await self.manager._generate_single_dummy(batch_position=0)
        
        # Verify registration and cleanup
        self.manager.priority_manager.register_dummy_request.assert_called_once_with(
            batch_position=0
        )
        self.manager.priority_manager.unregister_request.assert_called_once_with("dummy-123")
        self.assertNotIn("dummy-123", self.manager.active_requests)
    
    @unittest_run_loop
    async def test_generate_single_dummy_cancelled(self):
        """Test dummy generation when cancelled"""
        # Setup mocks
        self.manager.priority_manager.register_dummy_request = AsyncMock(
            return_value="dummy-456"
        )
        self.manager.priority_manager.unregister_request = AsyncMock()
        self.manager.priority_manager.attach_task = AsyncMock()
        
        self.manager.model_client = Mock()
        self.manager.model_client.models_by_name = {"Qwen/Qwen3-8B": [{}]}
        
        self.manager.prompt_generator = Mock()
        self.manager.prompt_generator.generate_prompt = Mock(return_value="test prompt")
        
        self.manager._inject_pow_data = Mock(return_value={"injected": "data"})
        
        # Mock execute to raise CancelledError
        self.manager._execute_dummy_request = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        
        with self.assertRaises(asyncio.CancelledError):
            await self.manager._generate_single_dummy()
        
        # Should still clean up
        self.manager.priority_manager.unregister_request.assert_called_once()
    
    @unittest_run_loop
    async def test_execute_dummy_request_success(self):
        """Test successful dummy request execution"""
        # Mock successful response
        mock_response = AsyncMock()
        mock_response.read = AsyncMock(return_value=b"response")
        
        mock_post = AsyncMock(return_value=mock_response)
        mock_post.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post.__aexit__ = AsyncMock()
        
        self.manager.session.post = mock_post
        self.manager.target_url = "http://test"
        self.manager.auth_headers = {}
        
        await self.manager._execute_dummy_request("dummy-789", {"test": "data"})
        
        # Should make request
        mock_post.assert_called_once()
        self.assertIn("/v1/completions", mock_post.call_args[0][0])
    
    @unittest_run_loop
    async def test_execute_dummy_request_retry(self):
        """Test dummy request with retry on failure"""
        # Mock failed then successful response
        self.manager.session.post = AsyncMock(
            side_effect=[
                Exception("Network error"),
                AsyncMock(__aenter__=AsyncMock(return_value=AsyncMock(read=AsyncMock())))
            ]
        )
        
        self.manager.target_url = "http://test"
        self.manager.auth_headers = {}
        
        with patch('components.proxy_with_priority.constants.DUMMY_RETRY_ATTEMPTS', 2):
            with patch('components.proxy_with_priority.constants.DUMMY_RETRY_BACKOFF', 0.01):
                await self.manager._execute_dummy_request("dummy-999", {"test": "data"})
        
        # Should retry
        self.assertEqual(self.manager.session.post.call_count, 2)
    
    @unittest_run_loop
    async def test_execute_dummy_request_cancelled_during_retry(self):
        """Test dummy request cancelled during retry backoff"""
        # Mock failed response
        self.manager.session.post = AsyncMock(side_effect=Exception("Network error"))
        self.manager.target_url = "http://test"
        self.manager.auth_headers = {}
        
        # Mock sleep to raise CancelledError
        with patch('asyncio.sleep', side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._execute_dummy_request("dummy-cancel", {"test": "data"})
    
    @unittest_run_loop
    async def test_monitor_loop(self):
        """Test monitor loop with priority management"""
        # Setup context
        self.manager.context.vdf_initialised = True
        self.manager.context.miner_initialised = True
        
        # Mock priority manager
        self.manager.priority_manager.cleanup_stale_requests = AsyncMock()
        self.manager.priority_manager.should_generate_dummy = AsyncMock(
            side_effect=[True, False]  # Generate once, then stop
        )
        self.manager.priority_manager.get_active_count = AsyncMock(
            return_value={'total': 5, 'external': 2, 'dummy': 3}
        )
        
        # Mock batch generation
        self.manager._generate_dummy_batch = AsyncMock()
        
        # Run one iteration
        with patch('asyncio.sleep', side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._monitor_loop()
        
        # Should have checked and generated dummy
        self.manager.priority_manager.should_generate_dummy.assert_called()
        self.manager.priority_manager.cleanup_stale_requests.assert_called()
    
    @unittest_run_loop
    async def test_priority_cleanup_loop(self):
        """Test priority cleanup loop"""
        self.manager.priority_manager.cleanup_stale_requests = AsyncMock(
            return_value=3  # Cleaned 3 requests
        )
        
        # Run one iteration
        with patch('asyncio.sleep', side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._priority_cleanup_loop()
        
        # Should have performed cleanup
        self.manager.priority_manager.cleanup_stale_requests.assert_called()
    
    def test_get_status(self):
        """Test status reporting with priority information"""
        # Mock priority manager statistics
        self.manager.priority_manager.get_statistics = Mock(
            return_value={
                'total_external': 100,
                'total_dummy': 500,
                'total_aborted': 50,
                'current_external': 2,
                'current_dummy': 3,
                'capacity_used': 5,
                'can_accept_external': True
            }
        )
        
        # Mock parent status
        with patch.object(super(PriorityRequestManager, self.manager), 
                         'get_status', 
                         return_value={'base': 'status'}):
            status = self.manager.get_status()
        
        self.assertIn('priority', status)
        self.assertEqual(status['priority']['total_external'], 100)
        self.assertEqual(status['priority']['total_dummy'], 500)
        self.assertEqual(status['priority']['total_aborted'], 50)
        self.assertEqual(status['priority']['current_external'], 2)
        self.assertEqual(status['priority']['current_dummy'], 3)


class TestPriorityRequestManagerIntegration(unittest.TestCase):
    """Integration tests for PriorityRequestManager"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.manager = PriorityRequestManager(self.context)
    
    def test_batch_size_configuration(self):
        """Test that batch size is properly configured"""
        from components import constants
        self.assertEqual(self.manager.priority_manager.batch_size, constants.BATCH_SIZE)
    
    def test_priority_manager_integration(self):
        """Test priority manager is properly integrated"""
        self.assertIsInstance(self.manager.priority_manager, RequestPriorityManager)
        self.assertEqual(
            self.manager.priority_manager.min_concurrent,
            self.manager.min_active
        )
    
    @patch('components.proxy_with_priority.logger')
    def test_logging_initialization(self, mock_logger):
        """Test that initialization is logged"""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        manager = PriorityRequestManager(context)
        
        mock_logger.info.assert_called_with(
            "[PriorityRequestManager] Initialized with priority management"
        )


if __name__ == '__main__':
    unittest.main()