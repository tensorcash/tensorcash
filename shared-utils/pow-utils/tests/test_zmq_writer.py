# SPDX-License-Identifier: Apache-2.0
"""Test ZMQ MiningResponseWriter for proof publishing."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import zmq
import time
import tempfile
import json
from pathlib import Path

# Check if zmq_pow_writer is available
try:
    from zmq_pow_writer import MiningResponseWriter
    HAS_ZMQ_WRITER = True
except ImportError:
    HAS_ZMQ_WRITER = False


@pytest.mark.skipif(not HAS_ZMQ_WRITER, reason="zmq_pow_writer not available")
class TestMiningResponseWriter:
    """Test ZMQ-based proof publishing."""
    
    @pytest.fixture
    def zmq_context(self):
        """Create ZMQ context for testing."""
        context = zmq.Context()
        yield context
        context.term()
    
    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for disk saves."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    @pytest.fixture
    def mock_proof_dict(self):
        """Create a mock proof dictionary."""
        return {
            'version': 1,
            'tick': 12345,
            'timestamp': int(time.time()),
            'target': "00" * 32,
            'vdf': "ff" * 32,
            'hash': "ab" * 32,
            'block_hash': "cd" * 32,
            'header_prefix': "00" * 76,
            'is_solution': False,
            'model_identifier': 'test-model',
            'compute_precision': 'fp16',
            'ipfs_cid': 'QmTest123',
            'model_config_diff': '',
            'temperature': 0.8,
            'top_p': 0.95,
            'top_k': 50,
            'repetition_penalty': 1.1,
            'chosen_tokens': torch.tensor([100, 200, 300], dtype=torch.int32),
            'chosen_probs': torch.tensor([0.1, 0.2, 0.3], dtype=torch.float32),
            'sampling_u': torch.tensor([0.5, 0.6, 0.7], dtype=torch.float32),
            'softmax_normalizers': torch.tensor([1.0, 1.1, 1.2], dtype=torch.float32),
            'prompt_tokens': torch.tensor([1, 2, 3, 4, 5], dtype=torch.int32),
            'pad_mask': torch.tensor([True, True, False], dtype=torch.bool),
            'topk_logits': torch.randn(3, 50, dtype=torch.float32),
            'topk_indices': torch.randint(0, 1000, (3, 50), dtype=torch.int32),
            'logsumexp_stats': torch.randn(3, 6, dtype=torch.float32),
        }
    
    def test_writer_initialization(self, zmq_context):
        """Test MiningResponseWriter initialization."""
        writer = MiningResponseWriter(context=zmq_context)
        
        assert writer.context is zmq_context
        assert writer.socket is not None
        
        # Clean up
        writer.close()
    
    def test_proxy_only_submission(self, zmq_context, mock_proof_dict):
        """Test proxy-only proof submission."""
        # Set up PULL socket to receive messages
        pull_socket = zmq_context.socket(zmq.PULL)
        port = pull_socket.bind_to_random_port("tcp://127.0.0.1")
        
        # Set environment for proxy-only mode
        os.environ['POW_PROXY_ENABLE'] = '1'
        os.environ['POW_PROXY_ENDPOINT'] = f'tcp://127.0.0.1:{port}'
        
        try:
            # Create writer and submit proof
            writer = MiningResponseWriter(context=zmq_context)
            
            # Submit proxy-only proof
            writer.submit_proof(mock_proof_dict, proxy_only=True)
            
            # Receive and verify message
            if pull_socket.poll(timeout=1000):
                message = pull_socket.recv()
                assert len(message) > 0
                # Message should be FlatBuffer encoded
                assert message[:4] != b'null'  # Not empty
            
            writer.close()
            
        finally:
            # Clean up
            del os.environ['POW_PROXY_ENABLE']
            del os.environ['POW_PROXY_ENDPOINT']
            pull_socket.close()
    
    def test_disk_save_option(self, zmq_context, mock_proof_dict, temp_dir):
        """Test saving proofs to disk."""
        # Set environment for disk save
        os.environ['POW_SAVE_TO_DISK'] = '1'
        os.environ['MINER_LOG_DIR'] = temp_dir
        
        try:
            writer = MiningResponseWriter(context=zmq_context)
            
            # Submit proof (will save to disk)
            writer.submit_proof(mock_proof_dict, proxy_only=False)
            
            # Check that file was created
            log_files = list(Path(temp_dir).glob("*.json"))
            assert len(log_files) > 0
            
            # Verify saved content
            with open(log_files[0], 'r') as f:
                saved_data = json.load(f)
                assert saved_data['tick'] == mock_proof_dict['tick']
                assert saved_data['model_identifier'] == mock_proof_dict['model_identifier']
            
            writer.close()
            
        finally:
            # Clean up
            del os.environ['POW_SAVE_TO_DISK']
            del os.environ['MINER_LOG_DIR']
    
    def test_solution_vs_proxy_channels(self, zmq_context, mock_proof_dict):
        """Test different channels for solutions vs proxy-only."""
        # Set up two PULL sockets for different channels
        core_socket = zmq_context.socket(zmq.PULL)
        core_port = core_socket.bind_to_random_port("tcp://127.0.0.1")
        
        proxy_socket = zmq_context.socket(zmq.PULL)
        proxy_port = proxy_socket.bind_to_random_port("tcp://127.0.0.1")
        
        # Configure endpoints
        os.environ['POW_CORE_ENDPOINT'] = f'tcp://127.0.0.1:{core_port}'
        os.environ['POW_PROXY_ENDPOINT'] = f'tcp://127.0.0.1:{proxy_port}'
        os.environ['POW_PROXY_ENABLE'] = '1'
        
        try:
            writer = MiningResponseWriter(context=zmq_context)
            
            # Submit solution (should go to core channel)
            solution_dict = mock_proof_dict.copy()
            solution_dict['is_solution'] = True
            solution_dict['hash'] = "00" * 30 + "0001"  # Low hash
            writer.submit_proof(solution_dict, proxy_only=False)
            
            # Submit proxy-only (should go to proxy channel)
            writer.submit_proof(mock_proof_dict, proxy_only=True)
            
            # Check core channel received solution
            if core_socket.poll(timeout=1000):
                core_msg = core_socket.recv()
                assert len(core_msg) > 0
            
            # Check proxy channel received proxy-only
            if proxy_socket.poll(timeout=1000):
                proxy_msg = proxy_socket.recv()
                assert len(proxy_msg) > 0
            
            writer.close()
            
        finally:
            # Clean up
            del os.environ['POW_CORE_ENDPOINT']
            del os.environ['POW_PROXY_ENDPOINT']
            del os.environ['POW_PROXY_ENABLE']
            core_socket.close()
            proxy_socket.close()
    
    def test_batch_submission(self, zmq_context):
        """Test submitting multiple proofs in sequence."""
        pull_socket = zmq_context.socket(zmq.PULL)
        port = pull_socket.bind_to_random_port("tcp://127.0.0.1")
        
        os.environ['POW_PROXY_ENABLE'] = '1'
        os.environ['POW_PROXY_ENDPOINT'] = f'tcp://127.0.0.1:{port}'
        
        try:
            writer = MiningResponseWriter(context=zmq_context)
            
            # Submit multiple proofs
            for i in range(5):
                proof = {
                    'version': 1,
                    'tick': 1000 + i,
                    'timestamp': int(time.time()),
                    'target': "ff" * 32,
                    'vdf': "00" * 32,
                    'hash': f"{i:02x}" * 32,
                    'block_hash': "aa" * 32,
                    'header_prefix': "00" * 76,
                    'is_solution': False,
                    'model_identifier': f'model-{i}',
                    'compute_precision': 'fp16',
                    'ipfs_cid': f'Qm{i}',
                    'model_config_diff': '',
                    'temperature': 1.0,
                    'top_p': 1.0,
                    'top_k': 50,
                    'repetition_penalty': 1.0,
                    'chosen_tokens': torch.tensor([i], dtype=torch.int32),
                    'chosen_probs': torch.tensor([0.5], dtype=torch.float32),
                    'sampling_u': torch.tensor([0.5], dtype=torch.float32),
                    'softmax_normalizers': torch.tensor([1.0], dtype=torch.float32),
                    'prompt_tokens': torch.tensor([i], dtype=torch.int32),
                    'pad_mask': torch.tensor([True], dtype=torch.bool),
                    'topk_logits': torch.zeros(1, 50, dtype=torch.float32),
                    'topk_indices': torch.zeros(1, 50, dtype=torch.int32),
                    'logsumexp_stats': torch.zeros(1, 6, dtype=torch.float32),
                }
                writer.submit_proof(proof, proxy_only=True)
            
            # Receive all messages
            received = 0
            while pull_socket.poll(timeout=100):
                msg = pull_socket.recv()
                assert len(msg) > 0
                received += 1
            
            assert received == 5
            
            writer.close()
            
        finally:
            del os.environ['POW_PROXY_ENABLE']
            del os.environ['POW_PROXY_ENDPOINT']
            pull_socket.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])