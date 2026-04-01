"""Comprehensive unit tests for ModelClient"""
import unittest
import asyncio
import sys
import os
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import httpx
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from components.model_synch import ModelClient


class TestModelClient(unittest.TestCase):
    def setUp(self):
        """Set up test client"""
        self.client = ModelClient()

    def test_initialization(self):
        """Test ModelClient initialization"""
        self.assertEqual(self.client.base_url, "http://localhost:8050")
        self.assertEqual(self.client.retry_attempts, 3)
        self.assertEqual(self.client.poll_interval, 300.0)
        self.assertFalse(self.client._initialized)
        self.assertEqual(len(self.client.models_by_hash), 0)
        self.assertEqual(len(self.client.models_by_name), 0)
        self.assertIsNone(self.client._update_task)

    @patch.dict(os.environ, {
        'MODEL_API_URL': 'http://test.local:9000',
        'MODEL_API_KEY': 'test_key',
        'MODEL_REQUIRE_AUTH': 'true',
        'MODEL_RETRY_ATTEMPTS': '5',
        'MODEL_RETRY_BACKOFF': '2.0',
        'MODEL_POLL_INTERVAL': '600'
    })
    def test_initialization_with_env_vars(self):
        """Test initialization with environment variables"""
        client = ModelClient()
        self.assertEqual(client.base_url, "http://test.local:9000")
        self.assertEqual(client.api_key, "test_key")
        self.assertTrue(client.require_auth)
        self.assertEqual(client.retry_attempts, 5)
        self.assertEqual(client.retry_backoff, 2.0)
        self.assertEqual(client.poll_interval, 600)
        self.assertIn("Authorization", client.headers)
        self.assertEqual(client.headers["Authorization"], "Bearer test_key")

    def test_headers_without_auth(self):
        """Test headers when auth is not required"""
        with patch.dict(os.environ, {'MODEL_REQUIRE_AUTH': 'false'}):
            client = ModelClient()
            self.assertNotIn("Authorization", client.headers)

    def test_get_model_by_hash_uninitialized(self):
        """Test getting model by hash when uninitialized"""
        with patch('components.model_synch.logger') as mock_logger:
            result = self.client.get_model_by_hash("test_hash")
            self.assertIsNone(result)
            mock_logger.warning.assert_called_once()

    def test_get_model_by_name_uninitialized(self):
        """Test getting model by name when uninitialized"""
        with patch('components.model_synch.logger') as mock_logger:
            result = self.client.get_model_by_name("test_model")
            self.assertIsNone(result)
            mock_logger.warning.assert_called_once()

    def test_get_status_uninitialized(self):
        """Test status reporting when uninitialized"""
        status = self.client.get_status()
        self.assertFalse(status["initialized"])
        self.assertEqual(status["models_loaded"], 0)
        self.assertIsNone(status["last_update_timestamp"])
        self.assertFalse(status["update_task_running"])
        self.assertEqual(status["base_url"], "http://localhost:8050")
        self.assertEqual(status["poll_interval"], 300.0)

    def test_get_status_initialized(self):
        """Test status reporting when initialized"""
        # Simulate initialization
        self.client._initialized = True
        self.client.models_by_hash = {"hash1": {"model_name": "test"}}
        self.client.last_update_timestamp = "2024-01-01T00:00:00"
        
        status = self.client.get_status()
        self.assertTrue(status["initialized"])
        self.assertEqual(status["models_loaded"], 1)
        self.assertEqual(status["last_update_timestamp"], "2024-01-01T00:00:00")

    def test_get_model_by_hash_initialized(self):
        """Test getting model by hash when initialized"""
        test_model = {"model_hash": "test_hash", "model_name": "test_model"}
        self.client._initialized = True
        self.client.models_by_hash = {"test_hash": test_model}
        
        result = self.client.get_model_by_hash("test_hash")
        self.assertEqual(result, test_model)
        
        # Test non-existent hash
        result = self.client.get_model_by_hash("nonexistent")
        self.assertIsNone(result)

    def test_get_model_by_name_initialized(self):
        """Test getting model by name when initialized"""
        test_model = {"model_hash": "test_hash", "model_name": "test_model"}
        self.client._initialized = True
        # models_by_name maps name → list of records (multi-commit per name).
        self.client.models_by_name = {"test_model": [test_model]}

        result = self.client.get_model_by_name("test_model")
        self.assertEqual(result, test_model)

        # Test non-existent name
        result = self.client.get_model_by_name("nonexistent")
        self.assertIsNone(result)


class TestModelClientAsync(unittest.IsolatedAsyncioTestCase):
    """Async tests for ModelClient"""
    
    async def asyncSetUp(self):
        """Set up async test client"""
        self.client = ModelClient()
        
    async def asyncTearDown(self):
        """Clean up after async tests"""
        if self.client._update_task:
            await self.client.stop()

    @patch('httpx.AsyncClient')
    async def test_fetch_models_success(self, mock_client_class):
        """Test successful model fetching"""
        # Setup mock response
        mock_response = Mock()
        mock_response.json.return_value = [
            {"model_hash": "hash1", "model_name": "model1"},
            {"model_hash": "hash2", "model_name": "model2"}
        ]
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        result = await self.client.fetch_models()
        
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["model_hash"], "hash1")
        mock_client.get.assert_called_once()

    @patch('httpx.AsyncClient')
    async def test_fetch_models_http_error(self, mock_client_class):
        """Test fetch_models with HTTP error"""
        mock_response = Mock()
        mock_response.status_code = 500
        
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "Server Error", request=Mock(), response=mock_response
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        # Should retry and eventually return empty list
        result = await self.client.fetch_models()
        self.assertEqual(result, [])
        
        # Should have made retry attempts
        self.assertEqual(mock_client.get.call_count, self.client.retry_attempts)

    @patch('httpx.AsyncClient')
    async def test_fetch_models_auth_error(self, mock_client_class):
        """Test fetch_models with authentication error"""
        mock_response = Mock()
        mock_response.status_code = 403
        
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "Forbidden", request=Mock(), response=mock_response
        )
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        # Should raise on auth error without retrying
        with self.assertRaises(httpx.HTTPStatusError):
            await self.client.fetch_models()

    @patch('httpx.AsyncClient')
    async def test_fetch_models_timeout(self, mock_client_class):
        """Test fetch_models with timeout"""
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.TimeoutException("Timeout")
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        result = await self.client.fetch_models()
        self.assertEqual(result, [])
        self.assertEqual(mock_client.get.call_count, self.client.retry_attempts)

    @patch('components.model_synch.ModelClient.fetch_models')
    async def test_update_models_success(self, mock_fetch):
        """Test successful model update"""
        mock_models = [
            {"model_hash": "hash1", "model_name": "model1"},
            {"model_hash": "hash2", "model_name": "model2"}
        ]
        mock_fetch.return_value = mock_models
        
        await self.client.update_models()
        
        self.assertTrue(self.client._initialized)
        self.assertEqual(len(self.client.models_by_hash), 2)
        self.assertEqual(len(self.client.models_by_name), 2)
        self.assertIn("hash1", self.client.models_by_hash)
        self.assertIn("model1", self.client.models_by_name)
        self.assertIsNotNone(self.client.last_update_time)
        self.assertIsNotNone(self.client.last_update_timestamp)

    async def test_broker_pushed_update_initializes_without_start_event(self):
        """Broker-registry mode can initialize without ModelClient.start()."""
        self.client._startup_event = None
        await self.client.update_from_payload(
            [
                {
                    "model_hash": "hash1",
                    "model_name": "Qwen/Qwen3-8B",
                    "model_commit": "commit1",
                    "status": 2,
                }
            ],
            source="broker-push",
        )

        self.assertTrue(self.client._initialized)
        self.assertEqual(len(self.client.models_by_hash), 1)
        self.assertIsNotNone(
            self.client.get_model_by_name_and_commit("Qwen/Qwen3-8B", "commit1")
        )

    @patch('components.model_synch.ModelClient.fetch_models')
    async def test_update_models_empty_response(self, mock_fetch):
        """Test update_models with empty response"""
        mock_fetch.return_value = []
        
        await self.client.update_models()
        
        # Should not initialize if no models returned
        self.assertFalse(self.client._initialized)
        self.assertEqual(len(self.client.models_by_hash), 0)

    @patch('components.model_synch.ModelClient.fetch_models')
    async def test_update_models_preserves_existing_on_empty(self, mock_fetch):
        """Test that existing models are preserved when fetch returns empty"""
        # Setup existing models
        self.client._initialized = True
        self.client.models_by_hash = {"existing": {"model_name": "existing_model"}}
        
        mock_fetch.return_value = []
        
        await self.client.update_models()
        
        # Should preserve existing models
        self.assertTrue(self.client._initialized)
        self.assertEqual(len(self.client.models_by_hash), 1)

    @patch('components.model_synch.ModelClient.fetch_models')
    async def test_update_models_handles_exception(self, mock_fetch):
        """Test update_models handles exceptions gracefully"""
        mock_fetch.side_effect = Exception("Network error")
        
        await self.client.update_models()
        
        # Should not crash and should not initialize
        self.assertFalse(self.client._initialized)

    @patch('components.model_synch.ModelClient.update_models')
    async def test_start_success(self, mock_update):
        """Test successful client start"""
        mock_update.return_value = None
        
        # Mock successful initialization
        async def mock_update_side_effect():
            self.client._initialized = True
            if self.client._startup_event:
                self.client._startup_event.set()
        
        mock_update.side_effect = mock_update_side_effect
        
        await self.client.start()
        
        self.assertIsNotNone(self.client._update_task)
        self.assertFalse(self.client._update_task.done())
        
        await self.client.stop()

    @patch('components.model_synch.ModelClient.fetch_models')
    async def test_start_timeout(self, mock_fetch):
        """Test client start timeout when model API is unreachable"""
        # Mock fetch_models to simulate network failure that never resolves
        async def slow_fetch():
            # Never complete - simulates network timeout/hang
            await asyncio.sleep(100)  # Never reached due to timeout
            return []
        
        mock_fetch.side_effect = slow_fetch
        
        # Use a very short timeout for the test (0.1s instead of 30s)
        with patch.object(self.client, 'start') as mock_start:
            async def fast_timeout_start():
                """Modified start method with shorter timeout for testing"""
                if self.client._update_task:
                    return
                
                if self.client._startup_event is None:
                    self.client._startup_event = asyncio.Event()
                self.client._update_task = asyncio.create_task(self.client._periodic_update_loop())
                
                try:
                    await asyncio.wait_for(self.client._startup_event.wait(), timeout=0.1)  # 100ms timeout
                except asyncio.TimeoutError:
                    raise RuntimeError("Failed to initialize model client - timeout")
            
            mock_start.side_effect = fast_timeout_start
            
            with self.assertRaises(RuntimeError) as ctx:
                await self.client.start()
            
            self.assertIn("timeout", str(ctx.exception))
            mock_fetch.assert_called()

    async def test_start_already_started(self):
        """Test starting client when already started"""
        # Mock a running task
        self.client._update_task = asyncio.create_task(asyncio.sleep(1))
        
        with patch('components.model_synch.logger') as mock_logger:
            await self.client.start()
            mock_logger.warning.assert_called_once()
        
        # Clean up
        self.client._update_task.cancel()
        try:
            await self.client._update_task
        except asyncio.CancelledError:
            pass

    async def test_stop_graceful(self):
        """Test graceful stop"""
        # Start a task
        self.client._update_task = asyncio.create_task(asyncio.sleep(10))
        
        await self.client.stop()
        
        self.assertIsNone(self.client._update_task)

    async def test_stop_not_running(self):
        """Test stop when not running"""
        await self.client.stop()  # Should not crash
        self.assertIsNone(self.client._update_task)

    @patch('components.model_synch.ModelClient.fetch_models')
    async def test_update_models_replaces_existing(self, mock_fetch):
        """Test that update_models replaces existing models"""
        # Setup existing models
        self.client._initialized = True
        self.client.models_by_hash = {"old_hash": {"model_name": "old_model"}}
        self.client.models_by_name = {"old_model": [{"model_hash": "old_hash"}]}
        
        # Mock new models
        new_models = [{"model_hash": "new_hash", "model_name": "new_model"}]
        mock_fetch.return_value = new_models
        
        await self.client.update_models()
        
        # Should replace old models
        self.assertEqual(len(self.client.models_by_hash), 1)
        self.assertIn("new_hash", self.client.models_by_hash)
        self.assertNotIn("old_hash", self.client.models_by_hash)


if __name__ == '__main__':
    unittest.main()
