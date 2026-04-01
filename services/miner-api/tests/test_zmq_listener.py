"""Comprehensive unit tests for ZMQListener"""
import unittest
import threading
import time
import sys
import os
import struct
import types
from unittest.mock import Mock, MagicMock, patch, call
import zmq

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Mock utils module
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

# Mock flatbuffers before import
if "flatbuffers" not in sys.modules:
    fb_mod = types.ModuleType("flatbuffers")
    fb_mod.Builder = Mock
    sys.modules["flatbuffers"] = fb_mod

# Mock proof module
if "proof" not in sys.modules:
    proof_mod = types.ModuleType("proof")
    
    class MockBlockHeader:
        @staticmethod
        def GetRootAsBlockHeader(buf, offset=0):
            return MockBlockHeader()
        
        def BlockHash(self):
            return "test_block_hash"
        
        def PrevHash(self):
            return "prev_hash"
        
        def MerkleRoot(self):
            return "merkle_root"
        
        def Version(self):
            return 1
        
        def Timestamp(self):
            return 1234567890
        
        def NBits(self):
            return 0x1d00ffff
        
        def Bits(self):
            return 0x1d00ffff
        
        def RequestId(self):
            return 42
    
    proof_mod.BlockHeader = MockBlockHeader
    sys.modules["proof"] = proof_mod
    sys.modules["proof.BlockHeader"] = proof_mod

from components.zmq_listener import ZMQListener
from components.context import LockFreeContext
from components import constants


class TestZMQListener(unittest.TestCase):
    """Test cases for ZMQListener"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.context = LockFreeContext("0" * 64, "ffff" * 16)
        self.vdf_service = Mock()
        self.vdf_service.restart_for_new_block = Mock()
        self.listener = ZMQListener(self.context, self.vdf_service, test_mode=False)
    
    def tearDown(self):
        """Clean up after tests"""
        if self.listener.running:
            self.listener.stop()
    
    def test_initialization(self):
        """Test ZMQListener initialization"""
        listener = ZMQListener(self.context, test_mode=True)
        
        self.assertEqual(listener.context, self.context)
        self.assertEqual(listener.pull_port, constants.ZMQ_PULL_PORT)
        self.assertEqual(listener.recv_timeout, constants.ZMQ_RECV_TIMEOUT_MS)
        self.assertEqual(listener.difficulty, constants.BASE_NBITS)
        self.assertFalse(listener.running)
        self.assertIsNone(listener.thread)
        self.assertIsNone(listener._zmq_context)
        self.assertIsNone(listener._socket)
        self.assertTrue(listener.test_mode)
    
    def test_set_vdf_service(self):
        """Test setting VDF service reference"""
        vdf_service_mock = Mock()
        self.listener.set_vdf_service(vdf_service_mock)
        self.assertEqual(self.listener.vdf_service, vdf_service_mock)
    
    @patch('components.zmq_listener.threading.Thread')
    def test_start(self, mock_thread_class):
        """Test starting ZMQ listener"""
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread
        
        self.listener.start()
        
        self.assertTrue(self.listener.running)
        mock_thread_class.assert_called_once_with(
            target=self.listener._run,
            daemon=True
        )
        mock_thread.start.assert_called_once()
        self.assertEqual(self.listener.thread, mock_thread)
    
    def test_start_already_running(self):
        """Test starting when already running"""
        self.listener.running = True
        
        with patch('components.zmq_listener.logger') as mock_logger:
            self.listener.start()
            mock_logger.warning.assert_called_with("ZMQ listener already running")
    
    def test_stop(self):
        """Test stopping ZMQ listener"""
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        self.listener.thread = mock_thread
        self.listener.running = True
        
        self.listener.stop()
        
        self.assertFalse(self.listener.running)
        mock_thread.join.assert_called_once_with(timeout=5.0)
    
    def test_stop_thread_timeout(self):
        """Test stop when thread doesn't stop cleanly"""
        mock_thread = Mock()
        mock_thread.is_alive.side_effect = [True, True]  # Still alive after join
        self.listener.thread = mock_thread
        self.listener.running = True
        
        with patch('components.zmq_listener.logger') as mock_logger:
            self.listener.stop()
            mock_logger.error.assert_called_with("ZMQ thread did not stop cleanly")
    
    @patch('zmq.Context')
    def test_run_normal_mode(self, mock_zmq_context_class):
        """Test _run method in normal mode"""
        # Setup ZMQ mocks
        mock_context = Mock()
        mock_socket = Mock()
        mock_zmq_context_class.return_value = mock_context
        mock_context.socket.return_value = mock_socket
        
        # Mock receive to return data then raise timeout
        test_data = b"test_mining_job"
        mock_socket.recv.side_effect = [test_data, zmq.error.Again()]
        
        # Mock process method
        self.listener._process_mining_job = Mock()
        
        # Run in a thread
        self.listener.running = True
        thread = threading.Thread(target=self.listener._run)
        thread.start()
        
        # Let it run briefly
        time.sleep(0.1)
        self.listener.running = False
        thread.join(timeout=1)
        
        # Verify socket setup
        mock_context.socket.assert_called_once_with(zmq.PULL)
        mock_socket.bind.assert_called_once_with(f"tcp://*:{self.listener.pull_port}")
        mock_socket.setsockopt.assert_called_once_with(zmq.RCVTIMEO, self.listener.recv_timeout)
        
        # Verify processing
        self.listener._process_mining_job.assert_called_once_with(test_data)
    
    @patch('components.zmq_listener.constants.GENESIS_GENERATOR', True)
    def test_run_genesis_mode(self):
        """Test _run method in genesis generation mode"""
        # Mock genesis generation
        self.listener._generate_genesis_job = Mock(return_value=b"genesis_job")
        self.listener._process_mining_job = Mock()
        
        # Run briefly
        self.listener.running = True
        
        with patch('time.sleep') as mock_sleep:
            mock_sleep.side_effect = [None, Exception("Stop")]  # Stop after one iteration
            
            with self.assertRaises(Exception):
                self.listener._run()
        
        # Should generate and process genesis job
        self.listener._generate_genesis_job.assert_called()
        self.listener._process_mining_job.assert_called_with(b"genesis_job")
    
    def test_run_test_mode_timeout(self):
        """Test _run method in test mode with timeout"""
        self.listener.test_mode = True
        
        # Mock socket to always timeout
        mock_socket = Mock()
        mock_socket.recv.side_effect = zmq.error.Again()
        
        with patch('zmq.Context') as mock_zmq_context:
            mock_context = Mock()
            mock_zmq_context.return_value = mock_context
            mock_context.socket.return_value = mock_socket
            
            # Mock test job generation
            self.listener._generate_test_job = Mock(return_value=b"test_job")
            self.listener._process_mining_job = Mock()
            
            # Run briefly
            self.listener.running = True
            
            with patch('time.sleep') as mock_sleep:
                mock_sleep.side_effect = [None] * 5 + [Exception("Stop")]
                
                with self.assertRaises(Exception):
                    self.listener._run()
            
            # Should generate test jobs on timeout
            self.assertTrue(self.listener._generate_test_job.called)
    
    @patch('components.zmq_listener.BlockHeader')
    def test_process_mining_job(self, mock_block_header_class):
        """Test processing a mining job"""
        # Setup mock header
        mock_header = Mock()
        mock_header.BlockHash.return_value = "new_block_hash"
        mock_header.PrevHash.return_value = "prev_hash"
        mock_header.MerkleRoot.return_value = "merkle_root"
        mock_header.Version.return_value = 3
        mock_header.Timestamp.return_value = 1234567890
        mock_header.NBits.return_value = 0x1d00ffff
        mock_header.Bits = lambda: 0x1d00ffff
        mock_header.RequestId.return_value = 100
        
        mock_block_header_class.BlockHeader.GetRootAsBlockHeader.return_value = mock_header
        
        # Process job
        test_fb_data = b"flatbuffer_data"
        self.listener._process_mining_job(test_fb_data)
        
        # Verify context was updated
        snapshot = self.context.read()
        self.assertEqual(snapshot.block_hash, "new_block_hash")
        self.assertEqual(snapshot.request_id, 100)
        
        # Verify VDF restart
        self.vdf_service.restart_for_new_block.assert_called_once_with("new_block_hash")
    
    def test_process_mining_job_no_block_change(self):
        """Test processing job when block doesn't change"""
        # Set initial block
        self.context.update_mining("same_block", "prefix", "target", 1)
        
        # Mock header with same block
        with patch('components.zmq_listener.BlockHeader') as mock_bh:
            mock_header = Mock()
            mock_header.BlockHash.return_value = "same_block"
            mock_header.PrevHash.return_value = "prev"
            mock_header.MerkleRoot.return_value = "merkle"
            mock_header.Version.return_value = 3
            mock_header.Timestamp.return_value = 123456
            mock_header.NBits.return_value = 0x1d00ffff
            mock_header.Bits = lambda: 0x1d00ffff
            mock_header.RequestId.return_value = 2
            
            mock_bh.BlockHeader.GetRootAsBlockHeader.return_value = mock_header
            
            self.listener._process_mining_job(b"data")
            
            # VDF should not restart for same block
            self.vdf_service.restart_for_new_block.assert_not_called()
    
    def test_generate_test_job(self):
        """Test test job generation"""
        with patch('time.time', return_value=1234567890):
            job_data = self.listener._generate_test_job()
        
        self.assertIsInstance(job_data, bytes)
        self.assertGreater(len(job_data), 0)
    
    @patch('components.zmq_listener.constants.GENESIS_GENERATOR', True)
    @patch('components.zmq_listener.generate_genesis_header_prefix')
    def test_generate_genesis_job(self, mock_genesis_func):
        """Test genesis job generation"""
        mock_genesis_func.return_value = (b"header_prefix", "coinbase_hash")
        
        with patch('time.time', return_value=1234567890):
            job_data = self.listener._generate_genesis_job()
        
        self.assertIsInstance(job_data, bytes)
        mock_genesis_func.assert_called()
    
    def test_get_status(self):
        """Test status reporting"""
        self.listener.running = True
        self.listener.thread = Mock(is_alive=Mock(return_value=True))
        
        status = self.listener.get_status()
        
        self.assertTrue(status["running"])
        self.assertTrue(status["thread_alive"])
        self.assertEqual(status["pull_port"], self.listener.pull_port)
        self.assertEqual(status["test_mode"], self.listener.test_mode)
    
    def test_cleanup_on_exception(self):
        """Test cleanup when exception occurs in run loop"""
        with patch('zmq.Context') as mock_zmq_context:
            mock_context = Mock()
            mock_socket = Mock()
            mock_zmq_context.return_value = mock_context
            mock_context.socket.return_value = mock_socket
            
            # Make recv raise an unexpected exception
            mock_socket.recv.side_effect = Exception("Unexpected error")
            
            self.listener.running = True
            
            with patch('components.zmq_listener.logger') as mock_logger:
                self.listener._run()
                
                # Should log the error
                mock_logger.exception.assert_called()
                
                # Should clean up
                mock_socket.close.assert_called_once()
                mock_context.term.assert_called_once()


class TestZMQListenerIntegration(unittest.TestCase):
    """Integration tests for ZMQListener"""
    
    def test_context_vdf_integration(self):
        """Test integration between context and VDF service"""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        vdf_service = Mock()
        listener = ZMQListener(context, vdf_service)
        
        # Mock a mining job
        with patch('components.zmq_listener.BlockHeader') as mock_bh:
            mock_header = Mock()
            mock_header.BlockHash.return_value = "integration_block"
            mock_header.PrevHash.return_value = "prev"
            mock_header.MerkleRoot.return_value = "merkle"
            mock_header.Version.return_value = 3
            mock_header.Timestamp.return_value = 123456
            mock_header.NBits.return_value = 0x1d00ffff
            mock_header.RequestId.return_value = 999
            # Make Bits return an actual integer that can be formatted
            mock_header.Bits = lambda: 0x1d00ffff
            
            mock_bh.BlockHeader.GetRootAsBlockHeader.return_value = mock_header
            
            listener._process_mining_job(b"data")
            
            # Verify context updated
            snapshot = context.read()
            self.assertEqual(snapshot.block_hash, "integration_block")
            
            # Verify VDF notified
            vdf_service.restart_for_new_block.assert_called_with("integration_block")
    
    def test_constants_usage(self):
        """Test that constants are properly used"""
        context = LockFreeContext("0" * 64, "ffff" * 16)
        listener = ZMQListener(context)
        
        self.assertEqual(listener.pull_port, constants.ZMQ_PULL_PORT)
        self.assertEqual(listener.recv_timeout, constants.ZMQ_RECV_TIMEOUT_MS)
        self.assertEqual(listener.difficulty, constants.BASE_NBITS)


if __name__ == '__main__':
    unittest.main()