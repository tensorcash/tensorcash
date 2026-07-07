# SPDX-License-Identifier: Apache-2.0
"""
Tests for CommonSamplerHelper class

This module tests the CommonSamplerHelper class which provides utilities for:
- Sequence cache initialization and management
- Context window extraction from ring buffers
- Cache updates with token streaming
- Stale sequence cleanup
- Row management and allocation
- PoW solution checking and processing
- Top-k sorting utilities
"""

import pytest
import torch
import time
import os
import tempfile
from unittest.mock import Mock, MagicMock, patch, call
from collections import deque
import sys
import struct
import hashlib

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock the vllm imports since we're testing in isolation
sys.modules['vllm'] = MagicMock()
sys.modules['vllm.sampling'] = MagicMock()
sys.modules['vllm.sampling.pow_utils'] = MagicMock()
sys.modules['vllm.sampling.zmq_pow_writer'] = MagicMock()
sys.modules['vllm.sampling.uint256_arithmetics'] = MagicMock()

# Import the class to test
from common_sampler_helper import CommonSamplerHelper


class TestCommonSamplerHelper:
    """Test suite for CommonSamplerHelper class"""
    
    @pytest.fixture
    def mock_owner(self):
        """Create a mock owner (sampler) object with necessary attributes"""
        owner = Mock()
        owner.window_size = 128
        owner.device = torch.device('cpu')
        owner.logger = Mock()
        owner.logger.log = Mock()
        owner.DEBUG_LOG = False
        owner.seq_caches = {}
        owner.seq_params = {}
        owner.row_manager = Mock()
        owner.row_manager.seqid_to_row = {}
        owner.row_manager.free_rows = deque(range(10))
        owner.row_manager.allocate_row = Mock(return_value=0)
        owner.row_manager.free_row = Mock(return_value=0)
        owner.row_manager.get_row = Mock(return_value=0)
        owner.row_manager.get_oldest_sequence = Mock(return_value=(None, None))
        owner.ring_buffers = Mock()
        owner.ring_buffers.clear_row = Mock()
        owner.ring_buffers.steps = torch.zeros(10, dtype=torch.long)
        owner.ring_buffers.get_window = Mock()
        owner.page_size = 512
        owner.prev_max_seq_len = 0
        owner.eos_token_id = 2
        owner._last_cleanup = 0
        owner.max_concurrency = 10
        owner.pow_hasher = Mock()
        owner.proof_writer = Mock()
        owner.submitter = Mock()
        owner._free_sequence = Mock()
        owner._init_sequence_cache = Mock()
        owner._process_solution = Mock()
        
        # Setup ring buffer attributes
        for attr in ("topk_logits", "topk_indices", "chosen_probs", 
                    "chosen_tokens", "attention_mask", "sampling_u",
                    "softmax_normalizers", "steps"):
            setattr(owner.ring_buffers, attr, torch.zeros(10, 128))
            
        return owner
    
    @pytest.fixture
    def helper(self, mock_owner):
        """Create a CommonSamplerHelper instance with mock owner"""
        return CommonSamplerHelper(mock_owner)
    
    def test_init(self, mock_owner):
        """Test initialization of CommonSamplerHelper"""
        helper = CommonSamplerHelper(mock_owner)
        assert helper.s == mock_owner
    
    def test_init_sequence_cache_empty_prompt(self, helper, mock_owner):
        """Test initializing sequence cache with empty prompt"""
        seq_id = "seq_001"
        prompt_tokens = []
        
        helper.init_sequence_cache(seq_id, prompt_tokens)
        
        assert seq_id in mock_owner.seq_caches
        cache = mock_owner.seq_caches[seq_id]
        assert cache["archive_list"] == []
        assert cache["pad_mask_list"] == []
        assert cache["ring"].shape == (128,)
        assert cache["ring_pos"] == 0
        assert cache["ring_filled"] == 0
        assert "last_updated" in cache
    
    def test_init_sequence_cache_short_prompt(self, helper, mock_owner):
        """Test initializing sequence cache with prompt shorter than window"""
        seq_id = "seq_002"
        prompt_tokens = [10, 20, 30, 40, 50]
        
        helper.init_sequence_cache(seq_id, prompt_tokens)
        
        cache = mock_owner.seq_caches[seq_id]
        assert cache["archive_list"] == prompt_tokens
        assert cache["pad_mask_list"] == [False] * 5
        assert cache["ring"][:5].tolist() == prompt_tokens
        assert cache["ring_pos"] == 5
        assert cache["ring_filled"] == 5
    
    def test_init_sequence_cache_long_prompt(self, helper, mock_owner):
        """Test initializing sequence cache with prompt longer than window"""
        seq_id = "seq_003"
        prompt_tokens = list(range(200))  # Longer than window_size=128
        
        helper.init_sequence_cache(seq_id, prompt_tokens)
        
        cache = mock_owner.seq_caches[seq_id]
        assert cache["archive_list"] == prompt_tokens
        assert len(cache["pad_mask_list"]) == 200
        # Ring should contain last 128 tokens
        expected_ring = prompt_tokens[-128:]
        assert cache["ring"].tolist() == expected_ring
        # When window is completely filled, ring_pos wraps to 0
        assert cache["ring_pos"] == 0  # 128 % 128 = 0
        assert cache["ring_filled"] == 128
    
    def test_init_sequence_cache_with_debug_log(self, helper, mock_owner):
        """Test that debug logging works when enabled"""
        mock_owner.DEBUG_LOG = True
        seq_id = "seq_debug"
        prompt_tokens = [1, 2, 3]
        
        helper.init_sequence_cache(seq_id, prompt_tokens)
        
        mock_owner.logger.log.assert_called_with(
            f"Initializing cache for seq_id={seq_id} (len=3)"
        )
    
    def test_get_context_windows_single_sequence(self, helper, mock_owner):
        """Test getting context windows for single sequence"""
        seq_id = "seq_004"
        tokens = list(range(50))
        helper.init_sequence_cache(seq_id, tokens)
        
        result = helper.get_context_windows([seq_id])
        
        assert result.shape == (1, 128)
        # When ring_pos = 50, the window is rearranged:
        # out[i, :W-rp] = r[rp:] and out[i, W-rp:] = r[:rp]
        cache = mock_owner.seq_caches[seq_id]
        rp = cache["ring_pos"]  # Should be 50
        # First 78 positions (128-50) get ring[50:128] which are all zeros
        # Last 50 positions get ring[0:50] which are the tokens
        expected = [0] * (128 - 50) + tokens
        assert result[0].tolist() == expected
    
    def test_get_context_windows_multiple_sequences(self, helper, mock_owner):
        """Test getting context windows for multiple sequences"""
        # Setup multiple sequences
        seq_ids = ["seq_A", "seq_B", "seq_C"]
        for i, sid in enumerate(seq_ids):
            tokens = list(range(i*10, (i+1)*10))
            helper.init_sequence_cache(sid, tokens)
        
        result = helper.get_context_windows(seq_ids)
        
        assert result.shape == (3, 128)
        # Check each sequence's window with proper rearrangement
        for i, sid in enumerate(seq_ids):
            cache = mock_owner.seq_caches[sid]
            rp = cache["ring_pos"]  # Should be 10 for each
            # Each has 10 tokens, so ring_pos = 10
            # First 118 positions (128-10) get ring[10:128] which are zeros
            # Last 10 positions get ring[0:10] which are the tokens
            expected = [0] * (128 - 10) + list(range(i*10, (i+1)*10))
            assert result[i].tolist() == expected
    
    def test_get_context_windows_with_wrapped_ring(self, helper, mock_owner):
        """Test context window extraction when ring buffer has wrapped"""
        seq_id = "seq_wrap"
        # Create a wrapped ring buffer scenario
        cache = {
            "ring": torch.arange(128),
            "ring_pos": 64,  # Ring has wrapped at position 64
            "ring_filled": 128,
            "archive_list": list(range(200)),
            "pad_mask_list": [False] * 200,
            "last_updated": time.time()
        }
        mock_owner.seq_caches[seq_id] = cache
        
        result = helper.get_context_windows([seq_id])
        
        # Should return tokens in correct order: [64-127, 0-63]
        expected = list(range(64, 128)) + list(range(0, 64))
        assert result[0].tolist() == expected
    
    def test_ensure_rows_allocates_new_row(self, helper, mock_owner):
        """Test ensure_rows allocates row for new sequence"""
        seq_id = "seq_new"
        prompt_mapping = {seq_id: [1, 2, 3]}
        
        helper.ensure_rows([seq_id], prompt_mapping)
        
        mock_owner.row_manager.allocate_row.assert_called_with(seq_id)
        mock_owner.ring_buffers.clear_row.assert_called_with(0)
        mock_owner._init_sequence_cache.assert_called_with(seq_id, [1, 2, 3])
    
    def test_ensure_rows_existing_row(self, helper, mock_owner):
        """Test ensure_rows skips sequences with existing rows"""
        seq_id = "seq_existing"
        mock_owner.row_manager.seqid_to_row[seq_id] = 5
        
        helper.ensure_rows([seq_id], {})
        
        mock_owner.row_manager.allocate_row.assert_not_called()
    
    def test_ensure_rows_evicts_old_sequence(self, helper, mock_owner):
        """Test ensure_rows evicts old sequence when no free rows"""
        seq_id = "seq_evict"
        old_seq = "seq_old"
        prompt_mapping = {seq_id: [1, 2, 3]}
        
        # First allocation fails, need to evict
        mock_owner.row_manager.allocate_row.side_effect = [None, 1]
        mock_owner.row_manager.get_oldest_sequence.return_value = (old_seq, 0)
        
        helper.ensure_rows([seq_id], prompt_mapping)
        
        assert mock_owner.row_manager.allocate_row.call_count == 2
        mock_owner._free_sequence.assert_called_with(old_seq)
    
    def test_update_caches_single_token(self, helper, mock_owner):
        """Test updating caches with single token"""
        seq_id = "seq_update"
        helper.init_sequence_cache(seq_id, [1, 2, 3])
        
        tokens = torch.tensor([42])
        page_crossed = helper.update_caches([seq_id], tokens)
        
        cache = mock_owner.seq_caches[seq_id]
        assert cache["archive_list"] == [1, 2, 3, 42]
        assert cache["ring"][3].item() == 42
        assert cache["ring_pos"] == 4
        assert cache["ring_filled"] == 4
        assert not page_crossed
    
    def test_update_caches_page_crossing(self, helper, mock_owner):
        """Test page crossing detection during cache update"""
        seq_id = "seq_page"
        # Create sequence near page boundary (page_size=512)
        initial_tokens = list(range(511))
        helper.init_sequence_cache(seq_id, initial_tokens)
        mock_owner.prev_max_seq_len = 511
        
        tokens = torch.tensor([999])
        page_crossed = helper.update_caches([seq_id], tokens)
        
        assert page_crossed  # Crossed from page 0 to page 1
        assert mock_owner.prev_max_seq_len == 512
    
    def test_update_caches_negative_token_assertion(self, helper, mock_owner):
        """Negative tokens trigger the assertion when debug asserts are on.

        The guard is gated behind _POW_DEBUG_ASSERTS (POW_DEBUG_ASSERTS env,
        read at import) so the hot path skips it in production; patch the module
        constant on so the test exercises the guard it names."""
        import common_sampler_helper
        seq_id = "seq_neg"
        helper.init_sequence_cache(seq_id, [1, 2])

        tokens = torch.tensor([-1])
        with patch.object(common_sampler_helper, "_POW_DEBUG_ASSERTS", True):
            with pytest.raises(AssertionError, match="Negative token found"):
                helper.update_caches([seq_id], tokens)
    
    def test_free_sequence_complete(self, helper, mock_owner):
        """Test complete sequence cleanup"""
        seq_id = "seq_free"
        helper.init_sequence_cache(seq_id, [1, 2, 3])
        mock_owner.seq_params[seq_id] = {"param": "value"}
        mock_owner.row_manager.free_row.return_value = 5
        # Add _req_id_to_sid as empty dict to avoid iteration error
        mock_owner._req_id_to_sid = {}
        
        helper.free_sequence(seq_id)
        
        assert seq_id not in mock_owner.seq_caches
        assert seq_id not in mock_owner.seq_params
        mock_owner.row_manager.free_row.assert_called_with(seq_id)
        # Clear row is called only if free_row returns a non-None value
        mock_owner.ring_buffers.clear_row.assert_called_with(5)
    
    def test_free_sequence_with_reverse_mapping(self, helper, mock_owner):
        """Test sequence cleanup with reverse request ID mapping"""
        seq_id = "seq_rev"
        helper.init_sequence_cache(seq_id, [1])
        mock_owner._req_id_to_sid = {
            "req1": seq_id,
            "req2": "other_seq"
        }
        
        helper.free_sequence(seq_id)
        
        assert "req1" not in mock_owner._req_id_to_sid
        assert "req2" in mock_owner._req_id_to_sid
    
    def test_check_eos_no_eos_tokens(self, helper, mock_owner):
        """Test check_eos when no EOS tokens present"""
        seq_ids = ["seq1", "seq2"]
        tokens = torch.tensor([10, 20])
        
        helper.check_eos(seq_ids, tokens)
        
        mock_owner.logger.log.assert_not_called()
    
    def test_check_eos_with_eos_token(self, helper, mock_owner):
        """Test check_eos frees sequence when EOS token encountered"""
        seq_ids = ["seq1", "seq2", "seq3"]
        for sid in seq_ids:
            helper.init_sequence_cache(sid, [1])
        
        tokens = torch.tensor([10, 2, 30])  # 2 is EOS token
        
        with patch.object(helper, 'free_sequence') as mock_free:
            helper.check_eos(seq_ids, tokens)
            mock_free.assert_called_once_with("seq2")
        
        mock_owner.logger.log.assert_called_with(
            "Sequence seq2 ended with EOS", "INFO"
        )
    
    def test_cleanup_stale_sequences_no_stale(self, helper, mock_owner):
        """Test cleanup when no sequences are stale"""
        mock_owner._last_cleanup = time.time()
        
        helper.cleanup_stale_sequences(max_age=300, interval=60)
        
        mock_owner.logger.log.assert_not_called()
    
    def test_cleanup_stale_sequences_removes_old(self, helper, mock_owner):
        """Test cleanup removes stale sequences"""
        old_time = time.time() - 400  # Older than max_age
        mock_owner.seq_caches = {
            "old_seq": {"last_updated": old_time},
            "new_seq": {"last_updated": time.time()}
        }
        mock_owner._last_cleanup = 0
        
        with patch.object(helper, 'free_sequence') as mock_free:
            helper.cleanup_stale_sequences(max_age=300, interval=60)
            mock_free.assert_called_once_with("old_seq")
        
        mock_owner.logger.log.assert_called_with(
            "Cleaned up stale seq old_seq", "INFO"
        )
    
    def test_reset_sampler_state(self, helper, mock_owner):
        """Test complete sampler state reset"""
        # Setup some state
        mock_owner.seq_caches = {"seq1": {}}
        mock_owner.seq_params = {"seq1": {}}
        mock_owner.row_manager.seqid_to_row = {"seq1": 0}
        
        # Make tensors have zero_ method
        for attr in ("topk_logits", "topk_indices", "chosen_probs",
                    "chosen_tokens", "attention_mask", "sampling_u",
                    "softmax_normalizers", "steps"):
            tensor = getattr(mock_owner.ring_buffers, attr)
            tensor.zero_ = Mock()
        
        result = helper.reset_sampler_state()
        
        assert result is True
        assert mock_owner.seq_caches == {}
        assert mock_owner.seq_params == {}
        assert mock_owner.row_manager.seqid_to_row == {}
        assert len(mock_owner.row_manager.free_rows) == 10
        
        # Check all ring buffers were zeroed
        for attr in ("topk_logits", "topk_indices", "chosen_probs",
                    "chosen_tokens", "attention_mask", "sampling_u",
                    "softmax_normalizers", "steps"):
            tensor = getattr(mock_owner.ring_buffers, attr)
            tensor.zero_.assert_called_once()
        
        assert mock_owner.logger.log.call_count >= 2
        mock_owner.logger.log.assert_any_call(
            "Performing complete sampler state reset", "INFO"
        )
        mock_owner.logger.log.assert_any_call(
            "Sampler state has been completely reset", "INFO"
        )
    
    def test_check_pow_solutions_no_solutions(self, helper, mock_owner):
        """Test PoW solution checking when no solutions found"""
        seq_ids = ["seq1", "seq2"]
        mock_owner.row_manager.get_row.return_value = 0
        mock_owner.ring_buffers.steps = torch.tensor([50, 100, 150])
        
        helper.check_pow_solutions(seq_ids)
        
        mock_owner._process_solution.assert_not_called()
    
    def test_check_pow_solutions_finds_solution(self, helper, mock_owner):
        """Test PoW solution checking when solution at window boundary"""
        seq_ids = ["seq1", "seq2"]
        # Use a function that returns consistent values for each seq_id
        def get_row_side_effect(sid):
            if sid == "seq1":
                return 0
            elif sid == "seq2":
                return 1
            return None
        
        mock_owner.row_manager.get_row.side_effect = get_row_side_effect
        # Set step 128 for row 0 and 256 for row 1 (both divisible by window_size=128)
        mock_owner.ring_buffers.steps = torch.tensor([128, 256, 150])
        
        helper.check_pow_solutions(seq_ids)
        
        # _process_solution should be called for both sequences since both have solutions
        assert mock_owner._process_solution.call_count == 2
        mock_owner._process_solution.assert_any_call("seq1", 0)
        mock_owner._process_solution.assert_any_call("seq2", 1)
    
    def test_ensure_sorted_topk_already_sorted(self, helper):
        """Test ensure_sorted_topk with already sorted tensors"""
        topk_logits = torch.tensor([
            [5.0, 4.0, 3.0, 2.0],
            [10.0, 9.0, 8.0, 7.0]
        ])
        topk_indices = torch.tensor([
            [0, 1, 2, 3],
            [4, 5, 6, 7]
        ])
        
        helper.ensure_sorted_topk(topk_logits, topk_indices)
        
        # Should remain unchanged
        assert topk_logits.tolist() == [[5.0, 4.0, 3.0, 2.0], [10.0, 9.0, 8.0, 7.0]]
        assert topk_indices.tolist() == [[0, 1, 2, 3], [4, 5, 6, 7]]
    
    def test_ensure_sorted_topk_needs_sorting(self, helper):
        """Test ensure_sorted_topk sorts unsorted tensors"""
        topk_logits = torch.tensor([
            [2.0, 4.0, 3.0, 5.0],  # Unsorted
            [10.0, 9.0, 8.0, 7.0]  # Sorted
        ])
        topk_indices = torch.tensor([
            [0, 1, 2, 3],
            [4, 5, 6, 7]
        ])
        
        helper.ensure_sorted_topk(topk_logits, topk_indices)
        
        # First row should be sorted
        assert topk_logits[0].tolist() == [5.0, 4.0, 3.0, 2.0]
        assert topk_indices[0].tolist() == [3, 1, 2, 0]
        # Second row unchanged
        assert topk_logits[1].tolist() == [10.0, 9.0, 8.0, 7.0]
        assert topk_indices[1].tolist() == [4, 5, 6, 7]
    
    def test_process_pow_params_no_groups(self, helper, mock_owner):
        """Test process_pow_params with no sequence groups"""
        metadata = Mock()
        metadata.seq_groups = []
        
        helper.process_pow_params(metadata)
        
        mock_owner.pow_hasher.update_from_payload.assert_not_called()
    
    def test_process_pow_params_with_pow_data(self, helper, mock_owner):
        """Test process_pow_params updates hasher with PoW data"""
        metadata = Mock()
        group = Mock()
        group.sampling_params = Mock()
        group.sampling_params.extra_args = {
            "pow": {"tick": 12345, "target": "0x1234"}
        }
        metadata.seq_groups = [group]
        
        helper.process_pow_params(metadata)
        
        mock_owner.pow_hasher.update_from_payload.assert_called_once_with(
            {"tick": 12345, "target": "0x1234"}
        )
    
    def test_log_prompt_data_empty_batch(self, helper, mock_owner):
        """Test log_prompt_data with empty batch"""
        metadata = Mock()
        metadata.seq_groups = []
        
        helper.log_prompt_data(metadata)
        
        mock_owner.logger.log.assert_called_with(
            "[DEBUG] Empty batch, nothing to log", "DEBUG"
        )
    
    def test_log_prompt_data_writes_to_file(self, helper, mock_owner):
        """Test log_prompt_data writes sequence data to file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_owner.logger.log_file_path = os.path.join(tmpdir, "main.log")
            
            metadata = Mock()
            group = Mock()
            group.seq_ids = ["seq1"]
            seq_data = Mock()
            seq_data.prompt_token_ids = (1, 2, 3)
            seq_data.prompt_token_ids_array = [1, 2, 3]
            seq_data.attention_mask = [1, 1, 1]
            seq_data.output_token_ids = [4, 5]
            group.seq_data = {"seq1": seq_data}
            metadata.seq_groups = [group]
            
            helper.log_prompt_data(metadata)
            
            log_file = os.path.join(tmpdir, "prompt_data.log")
            assert os.path.exists(log_file)
            
            with open(log_file, 'r') as f:
                content = f.read()
                assert "seq1" in content
                assert "(1, 2, 3)" in content
                assert "[4, 5]" in content
    
    def test_detect_real_inference_with_pow(self, helper):
        """Test detect_real_inference identifies PoW requests"""
        metadata = Mock()
        group = Mock()
        group.sampling_params = Mock()
        group.sampling_params.extra_args = {"pow": {"tick": 123}}
        metadata.seq_groups = [group]
        
        assert helper.detect_real_inference(metadata) is True
    
    def test_detect_real_inference_small_batch(self, helper):
        """Test detect_real_inference identifies small batches"""
        metadata = Mock()
        group = Mock()
        group.sampling_params = Mock()
        group.sampling_params.extra_args = {}
        group.seq_ids = ["seq1", "seq2"]
        metadata.seq_groups = [group]
        
        assert helper.detect_real_inference(metadata) is True
    
    def test_detect_real_inference_large_batch(self, helper):
        """Test detect_real_inference rejects large batches"""
        metadata = Mock()
        group = Mock()
        group.sampling_params = Mock()
        group.sampling_params.extra_args = {}
        group.seq_ids = ["seq1", "seq2", "seq3", "seq4", "seq5"]
        metadata.seq_groups = [group]
        
        assert helper.detect_real_inference(metadata) is False
    
    @patch.dict(os.environ, {"POW_PROXY_ENABLE": "0"})
    def test_process_solution_not_solution_no_proxy(self, helper, mock_owner):
        """Test process_solution skips non-solutions when proxy disabled"""
        seq_id = "seq_test"
        row = 0
        
        # Setup mocks
        mock_owner.ring_buffers.steps = torch.tensor([128])
        mock_owner.ring_buffers.get_window.return_value = {
            "tokens": torch.tensor([1, 2, 3])
        }
        mock_owner.pow_hasher.tick = 12345
        mock_owner.pow_hasher.target = torch.tensor([0xFF] * 32, dtype=torch.uint8)
        mock_owner.pow_hasher.v = torch.tensor([0xAA] * 32, dtype=torch.uint8)
        mock_owner.pow_hasher.h_b = torch.tensor([0xBB] * 32, dtype=torch.uint8)
        mock_owner.pow_hasher.header_prefix = None
        mock_owner.proof_writer.compute_precision = "fp16"
        
        # Mock the check_solution to return False (not a solution)
        mock_owner.pow_hasher.check_solution = Mock(return_value=torch.tensor([False]))
        
        # Mock the imported functions
        with patch('common_sampler_helper._tok_le_bytes') as mock_tok_bytes, \
             patch('common_sampler_helper._u32le') as mock_u32le, \
             patch('common_sampler_helper._str_bytes') as mock_str_bytes, \
             patch('common_sampler_helper._build_msg') as mock_build_msg, \
             patch('common_sampler_helper.sha256_many') as mock_sha256:
            
            mock_tok_bytes.return_value = torch.tensor([[1, 2, 3, 4]])
            mock_u32le.return_value = torch.tensor([0, 0, 0, 0])
            mock_str_bytes.return_value = torch.tensor([[1, 2]])
            mock_build_msg.return_value = torch.tensor([[1, 2, 3]])
            mock_sha256.return_value = torch.tensor([[0xAB] * 32], dtype=torch.uint8)
            
            helper.process_solution(seq_id, row)
            
            # Should not generate proof for non-solution with proxy disabled
            mock_owner.proof_writer.write_proof.assert_not_called()
            mock_owner.submitter.submit_solution.assert_not_called()
    
    @patch.dict(os.environ, {"POW_PROXY_ENABLE": "1"})
    def test_process_solution_proxy_audit_enabled(self, mock_owner):
        """Test process_solution submits audit when proxy enabled"""
        # Create helper after environment is patched
        helper = CommonSamplerHelper(mock_owner)
        seq_id = "seq_test"
        row = 0

        # Setup sequence cache
        mock_owner.seq_caches[seq_id] = {
            "archive_list": list(range(200)),
            "pad_mask_list": [False] * 200
        }

        # Setup pow_snapshot in seq_params (required by process_solution)
        mock_owner.seq_params[seq_id] = {
            "pow_snapshot": {
                "tick": 12345,
                "header_prefix": "cc" * 32,
                "vdf": "aa" * 32,
                "block_hash": "bb" * 32,
                "target": "ff" * 32,
                "ipfs_cid": "test_cid",
                "request_id": "req_123",
                "difficulty": 1000
            }
        }

        # Setup mocks for the complete flow
        mock_owner.ring_buffers.steps = torch.tensor([128])
        window_data = {"tokens": torch.tensor([1, 2, 3])}
        mock_owner.ring_buffers.get_window.return_value = window_data
        
        # Mock PoW components
        mock_owner.pow_hasher.tick = 12345
        mock_owner.pow_hasher.target = torch.tensor([0xFF] * 32, dtype=torch.uint8)
        mock_owner.pow_hasher.v = torch.tensor([0xAA] * 32, dtype=torch.uint8)
        mock_owner.pow_hasher.h_b = torch.tensor([0xBB] * 32, dtype=torch.uint8)
        mock_owner.pow_hasher.header_prefix = None
        mock_owner.pow_hasher.ipfs_cid = "test_cid"
        mock_owner.pow_hasher.request_id = "req_123"
        mock_owner.pow_hasher.difficulty = 1000
        mock_owner.pow_hasher.check_solution = Mock(return_value=torch.tensor([False]))
        
        mock_owner.proof_writer.compute_precision = "fp16"
        mock_owner.proof_writer.write_proof = Mock(return_value=(b"proof_blob", {"proof": "dict"}))
        
        # Mock the imported functions
        with patch('common_sampler_helper._tok_le_bytes') as mock_tok_bytes, \
             patch('common_sampler_helper._u32le') as mock_u32le, \
             patch('common_sampler_helper._str_bytes') as mock_str_bytes, \
             patch('common_sampler_helper._build_msg') as mock_build_msg, \
             patch('common_sampler_helper.sha256_many') as mock_sha256, \
             patch('common_sampler_helper.get_compact') as mock_get_compact, \
             patch('common_sampler_helper.hex_to_bytes_tensor') as mock_hex_to_bytes:

            mock_tok_bytes.return_value = torch.tensor([[1, 2, 3, 4]])
            mock_u32le.return_value = torch.tensor([0, 0, 0, 0])
            mock_str_bytes.return_value = torch.tensor([[1, 2]])
            mock_build_msg.return_value = torch.tensor([[1, 2, 3]])
            mock_sha256.return_value = torch.tensor([[0xAB] * 32], dtype=torch.uint8)
            mock_get_compact.return_value = 0x1d00ffff
            mock_hex_to_bytes.return_value = torch.tensor([0xCC] * 32, dtype=torch.uint8)

            mock_owner.submitter.submit_proof_for_audit = Mock()

            helper.process_solution(seq_id, row)

            # Should submit for audit even though not a solution
            mock_owner.submitter.submit_proof_for_audit.assert_called_once_with(
                req_id="req_123",
                proof_dict={"proof": "dict"}
            )
            # Should not submit as solution
            mock_owner.submitter.submit_solution.assert_not_called()
    
    @patch.dict(os.environ, {"POW_PROXY_ENABLE": "0"})
    def test_process_solution_valid_solution(self, helper, mock_owner):
        """Test process_solution submits valid solution to core-node"""
        seq_id = "seq_solution"
        row = 0
        
        # Setup sequence cache
        mock_owner.seq_caches[seq_id] = {
            "archive_list": list(range(200)),
            "pad_mask_list": [False] * 200
        }
        # Setup pow_snapshot in seq_params (required by process_solution)
        mock_owner.seq_params[seq_id] = {
            "completion_id": "comp_123",
            "pow_snapshot": {
                "tick": 12345,
                "header_prefix": "cc" * 32,
                "vdf": "aa" * 32,
                "block_hash": "bb" * 32,
                "target": "ff" * 32,
                "ipfs_cid": "test_cid",
                "request_id": "req_solution",
                "difficulty": 5000
            }
        }

        # Setup mocks
        mock_owner.ring_buffers.steps = torch.tensor([256])
        window_data = {"tokens": torch.tensor([1, 2, 3])}
        mock_owner.ring_buffers.get_window.return_value = window_data

        # Mock PoW components (these are still used by the mock)
        mock_owner.pow_hasher.check_solution = Mock(return_value=torch.tensor([True]))
        
        mock_owner.proof_writer.compute_precision = "fp32"
        proof_blob = b"valid_proof_blob"
        proof_dict = {"proof": "solution_dict"}
        mock_owner.proof_writer.write_proof = Mock(return_value=(proof_blob, proof_dict))
        
        # Mock the imported functions
        with patch('common_sampler_helper._tok_le_bytes') as mock_tok_bytes, \
             patch('common_sampler_helper._u32le') as mock_u32le, \
             patch('common_sampler_helper._str_bytes') as mock_str_bytes, \
             patch('common_sampler_helper._build_msg') as mock_build_msg, \
             patch('common_sampler_helper.sha256_many') as mock_sha256, \
             patch('common_sampler_helper.get_compact') as mock_get_compact, \
             patch('common_sampler_helper.hex_to_bytes_tensor') as mock_hex_to_bytes:

            mock_tok_bytes.return_value = torch.tensor([[1, 2, 3, 4]])
            mock_u32le.return_value = torch.tensor([0, 0, 0, 0])
            mock_str_bytes.return_value = torch.tensor([[1, 2]])
            mock_build_msg.return_value = torch.tensor([[1, 2, 3]])
            # Create digest with specific nonce in first 4 bytes
            digest = torch.zeros(1, 32, dtype=torch.uint8)
            digest[0, :4] = torch.tensor([0x12, 0x34, 0x56, 0x78])
            mock_sha256.return_value = digest
            mock_get_compact.return_value = 0x1d00ffff
            mock_hex_to_bytes.return_value = torch.tensor([0xCC] * 32, dtype=torch.uint8)

            mock_owner.submitter.submit_solution = Mock(return_value=True)

            helper.process_solution(seq_id, row)
            
            # Verify proof was written with correct parameters
            mock_owner.proof_writer.write_proof.assert_called_once()
            call_args = mock_owner.proof_writer.write_proof.call_args
            assert call_args[0][0] == seq_id
            assert call_args[0][1] == 256  # step_num
            assert call_args[0][4] == True  # is_solution
            assert call_args[1]["completion_id"] == "comp_123"
            
            # Verify solution was submitted
            mock_owner.submitter.submit_solution.assert_called_once()
            submit_args = mock_owner.submitter.submit_solution.call_args[1]
            assert submit_args["req_id"] == "req_solution"
            assert submit_args["nonce"] == 0x78563412  # Little-endian
            assert submit_args["adjusted_bits"] == 0x1d00ffff
            assert submit_args["difficulty"] == 5000
            assert submit_args["proof_dict"] == proof_dict
            
            # Verify success was logged
            mock_owner.logger.log.assert_any_call(
                "Solution submitted to core-node", "INFO"
            )
            mock_owner.logger.log.assert_any_call(
                f"Found PoW solution for sequence {seq_id}!", "INFO"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])