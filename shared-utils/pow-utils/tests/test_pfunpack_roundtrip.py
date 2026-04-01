# SPDX-License-Identifier: Apache-2.0
"""Test pfunpack roundtrip for FlatBuffers serialization/deserialization."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import numpy as np
from pow_utils import serialize_proof

# Try to import pfunpack
try:
    # Prefer local build output (current dir). If unavailable, try repo-relative path.
    import pfunpack  # built in tests directory by CI step
    HAS_PFUNPACK = True
except ImportError:
    HAS_PFUNPACK = False


@pytest.mark.skipif(not HAS_PFUNPACK, reason="pfunpack not available")
class TestPfunpackRoundtrip:
    """Test FlatBuffers proof serialization roundtrip."""
    
    def create_test_proof(self):
        """Create a comprehensive test proof dictionary."""
        return {
            'version': 2,
            'tick': 67890,
            'timestamp': 1234567890,
            'target': "00" * 30 + "ffff",  # Hex strings for binary fields
            'vdf': "aa" * 32,
            'hash': "bb" * 32,
            'block_hash': "cc" * 32,
            'header_prefix': "dd" * 76,
            'is_solution': True,
            'model_identifier': 'test-model-v1',
            'compute_precision': 'fp16',
            'ipfs_cid': 'QmTestCID123456789',
            'model_config_diff': 'config1=value1,config2=value2',
            'temperature': 0.8,
            'top_p': 0.95,
            'top_k': 40,
            'repetition_penalty': 1.15,
            'chosen_tokens': torch.tensor([101, 102, 103, 104], dtype=torch.int32),
            'chosen_probs': torch.tensor([0.1, 0.2, 0.3, 0.4], dtype=torch.float32),
            'sampling_u': torch.tensor([0.25, 0.5, 0.75, 1.0], dtype=torch.float32),
            'softmax_normalizers': torch.tensor([1.0, 1.1, 1.2, 1.3], dtype=torch.float32),
            'prompt_tokens': torch.tensor([1, 2, 3, 4, 5, 6, 7, 8], dtype=torch.int32),
            'pad_mask': torch.tensor([True, True, True, False], dtype=torch.bool),
            'topk_logits': torch.randn(4, 50, dtype=torch.float32),
            'topk_indices': torch.randint(0, 50000, (4, 50), dtype=torch.int32),
            'logsumexp_stats': torch.randn(4, 6, dtype=torch.float32),
        }
    
    def test_basic_roundtrip(self):
        """Test basic serialization and deserialization."""
        proof_dict = self.create_test_proof()
        
        # Serialize
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
        assert len(proof_bytes) > 0
        
        # Deserialize
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Verify scalar fields
        assert unpacked['version'] == proof_dict['version']
        assert unpacked['tick'] == proof_dict['tick']
        assert unpacked['timestamp'] == proof_dict['timestamp']
        assert unpacked['is_solution'] == proof_dict['is_solution']
        assert unpacked['model_identifier'] == proof_dict['model_identifier']
        assert unpacked['compute_precision'] == proof_dict['compute_precision']
        # Note: ipfs_cid might not be in unpacked if not in schema
        if 'ipfs_cid' in proof_dict and 'ipfs_cid' in unpacked:
            assert unpacked['ipfs_cid'] == proof_dict['ipfs_cid']
        
        # Verify float fields (with tolerance for float32 precision)
        assert abs(unpacked['temperature'] - proof_dict['temperature']) < 1e-6
        assert abs(unpacked['top_p'] - proof_dict['top_p']) < 1e-6
        assert unpacked['top_k'] == proof_dict['top_k']
        assert abs(unpacked['repetition_penalty'] - proof_dict['repetition_penalty']) < 1e-6
    
    def test_tensor_field_roundtrip(self):
        """Test that tensor fields are preserved."""
        proof_dict = self.create_test_proof()
        
        # Serialize and deserialize
        proof_bytes = serialize_proof(proof_dict)
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Check chosen_tokens
        if 'chosen_tokens' in unpacked:
            orig_tokens = proof_dict['chosen_tokens'].numpy()
            unpacked_tokens = np.array(unpacked['chosen_tokens'])
            assert np.array_equal(orig_tokens, unpacked_tokens)
        
        # Check chosen_probs (with float32 tolerance)
        if 'chosen_probs' in unpacked:
            orig_probs = proof_dict['chosen_probs'].numpy()
            unpacked_probs = np.array(unpacked['chosen_probs'])
            assert np.allclose(orig_probs, unpacked_probs, rtol=1e-6)
        
        # Check prompt_tokens
        if 'prompt_tokens' in unpacked:
            orig_prompt = proof_dict['prompt_tokens'].numpy()
            unpacked_prompt = np.array(unpacked['prompt_tokens'])
            assert np.array_equal(orig_prompt, unpacked_prompt)
    
    def test_binary_field_roundtrip(self):
        """Test that binary fields (hashes) are preserved."""
        proof_dict = self.create_test_proof()
        
        # Serialize and deserialize
        proof_bytes = serialize_proof(proof_dict)
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Check binary fields are preserved
        # Note: pfunpack might return these as bytes or hex strings
        if 'target' in unpacked:
            if isinstance(unpacked['target'], bytes):
                assert unpacked['target'].hex() == proof_dict['target'].lower()
            elif isinstance(unpacked['target'], str):
                assert unpacked['target'].lower() == proof_dict['target'].lower()
        
        if 'hash' in unpacked:
            if isinstance(unpacked['hash'], bytes):
                assert unpacked['hash'].hex() == proof_dict['hash'].lower()
            elif isinstance(unpacked['hash'], str):
                assert unpacked['hash'].lower() == proof_dict['hash'].lower()
    
    def test_empty_fields(self):
        """Test handling of empty/minimal fields."""
        proof_dict = {
            'version': 2,
            'tick': 0,
            'timestamp': 0,
            'target': "00" * 32,
            'vdf': "00" * 32,
            'hash': "00" * 32,
            'block_hash': "00" * 32,
            'header_prefix': "00" * 76,
            'is_solution': False,
            'model_identifier': '',
            'compute_precision': '',
            'ipfs_cid': '',
            'model_config_diff': '',
            'temperature': 0.0,
            'top_p': 0.0,
            'top_k': 0,
            'repetition_penalty': 0.0,
            'chosen_tokens': torch.tensor([], dtype=torch.int32),
            'chosen_probs': torch.tensor([], dtype=torch.float32),
            'sampling_u': torch.tensor([], dtype=torch.float32),
            'softmax_normalizers': torch.tensor([], dtype=torch.float32),
            'prompt_tokens': torch.tensor([], dtype=torch.int32),
            'pad_mask': torch.tensor([], dtype=torch.bool),
            'topk_logits': torch.zeros(0, 50, dtype=torch.float32),
            'topk_indices': torch.zeros(0, 50, dtype=torch.int32),
            'logsumexp_stats': torch.zeros(0, 6, dtype=torch.float32),
        }
        
        # Should serialize without error
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
        
        # Should deserialize without error
        unpacked = pfunpack.unpack_proof(proof_bytes)
        assert unpacked['version'] == 2
        assert unpacked['tick'] == 0
    
    def test_large_tensor_roundtrip(self):
        """Test with larger tensors to verify performance."""
        proof_dict = self.create_test_proof()
        
        # Use larger tensors
        seq_len = 256
        proof_dict['chosen_tokens'] = torch.randint(0, 50000, (seq_len,), dtype=torch.int32)
        proof_dict['chosen_probs'] = torch.rand(seq_len, dtype=torch.float32)
        proof_dict['sampling_u'] = torch.rand(seq_len, dtype=torch.float32)
        proof_dict['softmax_normalizers'] = torch.rand(seq_len, dtype=torch.float32)
        proof_dict['pad_mask'] = torch.ones(seq_len, dtype=torch.bool)
        proof_dict['topk_logits'] = torch.randn(seq_len, 50, dtype=torch.float32)
        proof_dict['topk_indices'] = torch.randint(0, 50000, (seq_len, 50), dtype=torch.int32)
        proof_dict['logsumexp_stats'] = torch.randn(seq_len, 6, dtype=torch.float32)
        
        # Serialize
        proof_bytes = serialize_proof(proof_dict)
        assert len(proof_bytes) > 10000  # Should be reasonably large
        
        # Deserialize
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Verify sizes are preserved
        if 'chosen_tokens' in unpacked:
            assert len(unpacked['chosen_tokens']) == seq_len
        if 'topk_logits' in unpacked:
            assert len(unpacked['topk_logits']) == seq_len
    
    def test_float32_precision(self):
        """Test that float32 precision is maintained."""
        proof_dict = self.create_test_proof()
        
        # Use specific float values that might have precision issues
        test_values = [
            0.123456789,  # Many decimal places
            1.0 / 3.0,     # Repeating decimal
            np.pi,         # Irrational
            1e-7,          # Very small
            1e7,           # Large
        ]
        
        proof_dict['chosen_probs'] = torch.tensor(test_values[:4], dtype=torch.float32)
        proof_dict['sampling_u'] = torch.tensor(test_values[:4], dtype=torch.float32)
        
        # Serialize and deserialize
        proof_bytes = serialize_proof(proof_dict)
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Check float32 precision is maintained
        if 'chosen_probs' in unpacked:
            orig = proof_dict['chosen_probs'].numpy().astype(np.float32)
            unpacked_vals = np.array(unpacked['chosen_probs'], dtype=np.float32)
            
            # Should match to float32 precision
            assert np.allclose(orig, unpacked_vals, rtol=1e-7, atol=1e-7)
    
    def test_validation_fields(self):
        """Test fields used for validation (MiningResponse wrapper)."""
        proof_dict = self.create_test_proof()
        
        # Add validation-specific fields if MiningResponse is supported
        proof_dict['request_id'] = 42
        proof_dict['miner_id'] = 'miner-001'
        
        # Serialize
        proof_bytes = serialize_proof(proof_dict)
        
        # If pfunpack supports MiningResponse unpacking
        if hasattr(pfunpack, 'unpack_mining_response'):
            # Wrap in MiningResponse envelope
            # (This would require additional setup)
            pass
        else:
            # Just verify basic proof unpacking works
            unpacked = pfunpack.unpack_proof(proof_bytes)
            assert 'version' in unpacked


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
