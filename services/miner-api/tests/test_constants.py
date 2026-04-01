"""Comprehensive unit tests for components.constants"""
import unittest
import sys
import os
import types
import importlib
from unittest.mock import Mock, patch


class TestConstants(unittest.TestCase):
    """Test cases for constants module"""
    
    @classmethod
    def setUpClass(cls):
        """Set up mock dependencies once for all tests"""
        cls._install_uint256_mock()
        
        # Add src to path for imports
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
        
        # Import the module
        cls.constants = importlib.import_module('components.constants')
    
    @staticmethod
    def _install_uint256_mock():
        """Install mock for uint256_arithmetics"""
        if "utils.uint256_arithmetics" not in sys.modules:
            mod = types.ModuleType("utils.uint256_arithmetics")
            def set_compact(x):
                return x
            def get_compact(x):
                return x
            def adjust_nbits_by_multiplier(bits, mult, default=None):
                return {"target_bytes": b"\xff" * 32, "nbits": 0x1d00ffff}
            
            mod.set_compact = set_compact
            mod.get_compact = get_compact
            mod.adjust_nbits_by_multiplier = adjust_nbits_by_multiplier

            # Ensure utils package exists
            pkg = types.ModuleType("utils")
            sys.modules["utils"] = pkg
            sys.modules["utils.uint256_arithmetics"] = mod

    def test_environment_variable_loading(self):
        """Test that environment variables are loaded with correct defaults"""
        # Test some key environment variables have expected defaults
        self.assertEqual(self.constants.HTTP_HOST, "0.0.0.0")
        self.assertEqual(self.constants.HTTP_PORT, 8080)
        self.assertEqual(self.constants.TARGET_URL, "http://localhost:8000")
        self.assertEqual(self.constants.API_KEY, "dev-secret")
        
        # Test ZMQ configuration
        self.assertEqual(self.constants.ZMQ_PULL_PORT, 6000)
        self.assertEqual(self.constants.ZMQ_RECV_TIMEOUT_MS, 6000000)
        
        # Test VDF configuration
        self.assertEqual(self.constants.VDF_DISCRIMINANT_SIZE, 1024)
        self.assertEqual(self.constants.VDF_CHECKPOINT_SIZE, 32768)
        self.assertEqual(self.constants.VDF_UPDATE_INTERVAL, 0.1)

    @patch.dict(os.environ, {
        'HTTP_PORT': '9090',
        'ZMQ_PULL_PORT': '7000',
        'MODEL_RETRY_ATTEMPTS': '5',
        'PRIORITY_MODE': 'true'
    })
    def test_environment_variable_override(self):
        """Test that environment variables can be overridden"""
        # Re-import to get fresh environment values
        import importlib
        importlib.reload(self.constants)
        
        self.assertEqual(self.constants.HTTP_PORT, 9090)
        self.assertEqual(self.constants.ZMQ_PULL_PORT, 7000)
        self.assertEqual(self.constants.MODEL_RETRY_ATTEMPTS, 5)
        self.assertTrue(self.constants.PRIORITY_MODE)

    def test_base_nbits_parsing(self):
        """Test BASE_NBITS parsing with different formats"""
        # Test that BASE_NBITS is parsed correctly
        self.assertIsInstance(self.constants.BASE_NBITS, int)
        
        # Test invalid BASE_NBITS would raise error
        with patch.dict(os.environ, {'BASE_NBITS': 'invalid'}):
            with self.assertRaises(RuntimeError) as ctx:
                importlib.reload(self.constants)
            self.assertIn("Invalid BASE_NBITS", str(ctx.exception))

    def test_model_config_dataclass(self):
        """Test ModelConfig dataclass functionality"""
        config = self.constants.ModelConfig(
            model_hash="test_hash",
            model_name="test_model",
            model_commit="test_commit",
            difficulty=1000000,
            ipfs_cid="Qm123",
            target_adj="custom_target"
        )
        
        self.assertEqual(config.model_hash, "test_hash")
        self.assertEqual(config.model_name, "test_model")
        self.assertEqual(config.difficulty, 1000000)
        self.assertEqual(config.ipfs_cid, "Qm123")
        self.assertEqual(config.target_adjustment, "custom_target")
        
        # Test default target adjustment
        config_no_target = self.constants.ModelConfig(
            model_hash="hash",
            model_name="model",
            model_commit="commit",
            difficulty=1000
        )
        expected_default = "7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        self.assertEqual(config_no_target.target_adjustment, expected_default)

    def test_get_model_config_fallback(self):
        """Test get_model_config with fallback behavior"""
        # Test unknown model falls back to default
        config = self.constants.get_model_config("UnknownModel")
        self.assertEqual(config.model_name, self.constants.DEFAULT_MODEL_CONFIG.model_name)
        
        # Test known fallback model
        config = self.constants.get_model_config("Qwen/Qwen3-8B")
        self.assertEqual(config.model_name, "Qwen/Qwen3-8B")
        self.assertEqual(config.difficulty, self.constants.DEFAULT_DIFFICULTY)

    def test_get_model_config_with_client(self):
        """Test get_model_config with model client"""
        # constants.get_model_config calls get_models_by_name (plural) and
        # picks the highest-block_height record when multiple are present.
        mock_client = Mock()
        mock_client.get_models_by_name.return_value = [{
            "model_hash": "client_hash",
            "model_name": "client_model",
            "model_commit": "client_commit",
            "difficulty": 123456,
            "cid": "QmClientCID",
            "txid": "client_tx",
            "block_hash": "client_block",
            "block_height": 100
        }]

        config = self.constants.get_model_config("client_model", model_client=mock_client)
        self.assertEqual(config.model_hash, "client_hash")
        self.assertEqual(config.model_name, "client_model")
        self.assertEqual(config.difficulty, 123456)
        self.assertEqual(config.ipfs_cid, "QmClientCID")

        mock_client.get_models_by_name.assert_called_once_with("client_model")

    def test_get_model_config_client_returns_none(self):
        """Test get_model_config when client has no records for that name"""
        mock_client = Mock()
        mock_client.get_models_by_name.return_value = []

        # Should fall back to static config
        config = self.constants.get_model_config("Qwen/Qwen3-8B", model_client=mock_client)
        self.assertEqual(config.model_name, "Qwen/Qwen3-8B")

    def test_get_model_config_by_hash_with_client(self):
        """Test get_model_config_by_hash with model client"""
        mock_client = Mock()
        mock_client.get_model_by_hash.return_value = {
            "model_hash": "test_hash",
            "model_name": "hash_model",
            "model_commit": "hash_commit",
            "difficulty": 789,
            "cid": None
        }
        
        config = self.constants.get_model_config_by_hash("test_hash", model_client=mock_client)
        self.assertEqual(config.model_hash, "test_hash")
        self.assertEqual(config.model_name, "hash_model")
        self.assertEqual(config.difficulty, 789)
        self.assertIsNone(config.ipfs_cid)

    def test_get_model_config_by_hash_fallback(self):
        """Test get_model_config_by_hash with fallback configs"""
        # Test with fallback hash
        fallback_hash = self.constants.DEFAULT_MODEL_CONFIG.model_hash
        config = self.constants.get_model_config_by_hash(fallback_hash)
        self.assertIsNotNone(config)
        self.assertEqual(config.model_hash, fallback_hash)
        
        # Test with unknown hash
        config = self.constants.get_model_config_by_hash("unknown_hash")
        self.assertIsNone(config)

    def test_settings_dataclass(self):
        """Test Settings dataclass"""
        settings = self.constants.Settings.load()
        
        self.assertEqual(settings.http_host, self.constants.HTTP_HOST)
        self.assertEqual(settings.http_port, self.constants.HTTP_PORT)
        self.assertEqual(settings.target_url, self.constants.TARGET_URL)
        self.assertEqual(settings.log_level, self.constants.LOG_LEVEL)

    def test_boolean_environment_parsing(self):
        """Test boolean environment variable parsing"""
        # Test various boolean representations
        with patch.dict(os.environ, {'MODEL_REQUIRE_AUTH': 'true'}):
            importlib.reload(self.constants)
            self.assertTrue(self.constants.MODEL_REQUIRE_AUTH)
        
        with patch.dict(os.environ, {'MODEL_REQUIRE_AUTH': '1'}):
            importlib.reload(self.constants)
            self.assertTrue(self.constants.MODEL_REQUIRE_AUTH)
        
        with patch.dict(os.environ, {'MODEL_REQUIRE_AUTH': 'yes'}):
            importlib.reload(self.constants)
            self.assertTrue(self.constants.MODEL_REQUIRE_AUTH)
        
        with patch.dict(os.environ, {'MODEL_REQUIRE_AUTH': 'false'}):
            importlib.reload(self.constants)
            self.assertFalse(self.constants.MODEL_REQUIRE_AUTH)

    def test_llama_cpp_defaults_disable_vllm_xargs(self):
        """llama.cpp should default back to extra_sampling_params unless overridden."""
        with patch.dict(os.environ, {'LLAMA_CPP': 'True'}, clear=False):
            os.environ.pop('USE_VLLM_XARGS', None)
            importlib.reload(self.constants)
            self.assertTrue(self.constants.LLAMA_CPP)
            self.assertFalse(self.constants.USE_VLLM_XARGS)

        with patch.dict(os.environ, {'LLAMA_CPP': 'True', 'USE_VLLM_XARGS': 'true'}, clear=False):
            importlib.reload(self.constants)
            self.assertTrue(self.constants.USE_VLLM_XARGS)

        importlib.reload(self.constants)

    def test_fallback_model_configs_structure(self):
        """Test that fallback model configurations are properly structured"""
        self.assertIn("Qwen/Qwen3-8B", self.constants.FALLBACK_MODEL_CONFIGS)
        
        default_config = self.constants.FALLBACK_MODEL_CONFIGS["Qwen/Qwen3-8B"]
        self.assertIsInstance(default_config, self.constants.ModelConfig)
        self.assertEqual(default_config.model_name, "Qwen/Qwen3-8B")
        self.assertEqual(default_config.difficulty, self.constants.DEFAULT_DIFFICULTY)
        self.assertIsNotNone(default_config.ipfs_cid)

    def test_proof_cache_configuration(self):
        """Test proof cache related configuration"""
        self.assertTrue(self.constants.PROOF_CACHE_ENABLED)
        self.assertEqual(self.constants.PROOF_CACHE_TTL_SECONDS, 900)
        self.assertEqual(self.constants.PROOF_CACHE_MAX_SIZE_MB, 500)
        self.assertEqual(self.constants.PROOF_COLLECTOR_PORT, 7002)


if __name__ == '__main__':
    unittest.main()
