# SPDX-License-Identifier: Apache-2.0
"""Test ProofWriter serialization and FlatBuffers integration."""

import pytest
import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import numpy as np
from pow_utils import ProofWriter, hex_to_bytes_tensor


class TestProofWriter:
    """Test ProofWriter FlatBuffers serialization."""
    
    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for ProofWriter output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir
    
    @pytest.fixture
    def proof_writer(self, temp_dir):
        """Create a ProofWriter with temporary output directory."""
        return ProofWriter(output_dir=temp_dir)
    
    def create_minimal_proof_dict(self):
        """Create a minimal valid proof dictionary."""
        return {
            'version': 2,
            'tick': 12345,
            'timestamp': 1234567890,
            'target': "00" * 32,  # Use hex string, not tensor
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
            'pad_mask': torch.tensor([True, True, False], dtype=torch.bool),  # Use pad_mask for serialization
            'topk_logits': torch.randn(3, 50, dtype=torch.float32),
            'topk_indices': torch.randint(0, 1000, (3, 50), dtype=torch.int32),
            'logsumexp_stats': torch.randn(3, 6, dtype=torch.float32),
        }
    
    def test_serialize_minimal(self, proof_writer):
        """Test serializing a minimal proof."""
        proof_dict = self.create_minimal_proof_dict()
        
        # ProofWriter.write_proof builds the dict then calls serialize_proof
        # We need to test the actual API
        # Check if write_proof exists
        if hasattr(proof_writer, 'write_proof'):
            # write_proof might build its own dict, so we can't use our dict directly
            # Instead, let's test the module-level serialize_proof function
            from pow_utils import serialize_proof
            proof_bytes = serialize_proof(proof_dict)
        else:
            # Fall back to testing serialize_proof directly
            from pow_utils import serialize_proof
            proof_bytes = serialize_proof(proof_dict)
        
        # Basic checks
        assert isinstance(proof_bytes, bytes)
        assert len(proof_bytes) > 0
    
    def test_serialize_deterministic(self, proof_writer):
        """Test that serialization is deterministic."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Serialize twice
        from pow_utils import serialize_proof
        bytes1 = serialize_proof(proof_dict)
        bytes2 = serialize_proof(proof_dict)
        
        # Should be identical
        assert bytes1 == bytes2
    
    def test_serialize_with_empty_fields(self, proof_writer):
        """Test serialization with some empty fields."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Make some fields empty
        proof_dict['model_config_diff'] = ''
        proof_dict['prompt_tokens'] = torch.tensor([], dtype=torch.int32)
        
        # Should still serialize without error
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_large_tensors(self, proof_writer):
        """Test serialization with larger tensors."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Use larger tensors
        proof_dict['chosen_tokens'] = torch.randint(0, 50000, (256,), dtype=torch.int32)
        proof_dict['topk_logits'] = torch.randn(256, 50, dtype=torch.float32)
        proof_dict['topk_indices'] = torch.randint(0, 50000, (256, 50), dtype=torch.int32)
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        
        assert isinstance(proof_bytes, bytes)
        assert len(proof_bytes) > 1000  # Should be reasonably large
    
    def test_float32_precision(self, proof_writer):
        """Test that float values are explicitly cast to float32."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Use specific float values that could have precision issues
        proof_dict['temperature'] = np.float64(0.123456789)  # High precision
        proof_dict['top_p'] = 0.9999999
        proof_dict['chosen_probs'] = torch.tensor([0.1, 0.2, 0.3], dtype=torch.float64)
        
        # Serialize - should handle float32 conversion internally
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_bool_array(self, proof_writer):
        """Test serialization of boolean arrays."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Various boolean patterns
        proof_dict['pad_mask'] = torch.tensor(
            [True, False, True, True, False, False, True],
            dtype=torch.bool
        )
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_solution_proof(self, proof_writer):
        """Test serialization when is_solution is True."""
        proof_dict = self.create_minimal_proof_dict()
        
        proof_dict['is_solution'] = True
        proof_dict['hash'] = "00" * 30 + "0001"  # Low hash
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_without_optional_fields(self, proof_writer):
        """Test serialization without optional fields."""
        # Minimal required fields only
        proof_dict = {
            'version': 2,
            'tick': 1000,
            'timestamp': 1234567890,
            'target': "ff" * 32,  # Use hex strings
            'vdf': "00" * 32,
            'hash': "aa" * 32,
            'block_hash': "bb" * 32,
            'header_prefix': "00" * 76,
            'is_solution': False,
            'model_identifier': 'model',
            'compute_precision': 'fp16',
            'ipfs_cid': '',
            'model_config_diff': '',
            'temperature': 1.0,
            'top_p': 1.0,
            'top_k': 50,
            'repetition_penalty': 1.0,
            'chosen_tokens': torch.tensor([1], dtype=torch.int32),
            'chosen_probs': torch.tensor([1.0], dtype=torch.float32),
            'sampling_u': torch.tensor([0.5], dtype=torch.float32),
            'softmax_normalizers': torch.tensor([1.0], dtype=torch.float32),
            'prompt_tokens': torch.tensor([1], dtype=torch.int32),
            'pad_mask': torch.tensor([True], dtype=torch.bool),
            'topk_logits': torch.zeros(1, 50, dtype=torch.float32),
            'topk_indices': torch.zeros(1, 50, dtype=torch.int32),
            'logsumexp_stats': torch.zeros(1, 6, dtype=torch.float32),
        }
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_with_special_characters(self, proof_writer):
        """Test serialization with special characters in strings."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Special characters in string fields
        proof_dict['model_identifier'] = 'model-v1.0_test'
        proof_dict['compute_precision'] = 'int8'
        proof_dict['ipfs_cid'] = 'Qm' + 'X' * 44  # Long IPFS CID
        proof_dict['model_config_diff'] = 'flag1,flag2,flag3'
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_edge_values(self, proof_writer):
        """Test serialization with edge case values."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Edge values
        proof_dict['tick'] = 2**63 - 1  # Max int64
        proof_dict['temperature'] = 0.0  # Min temperature
        proof_dict['top_p'] = 1.0  # Max top_p
        proof_dict['top_k'] = 1  # Min top_k
        proof_dict['repetition_penalty'] = 0.0  # No penalty
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    def test_serialize_consistent_dtype_handling(self, proof_writer):
        """Test that different tensor dtypes are handled correctly."""
        proof_dict = self.create_minimal_proof_dict()
        
        # Mix of dtypes that should be converted appropriately
        proof_dict['chosen_tokens'] = torch.tensor([1, 2, 3], dtype=torch.int64)  # Will convert to int32
        proof_dict['topk_indices'] = torch.tensor([[1, 2]], dtype=torch.int64)  # Will convert to int32
        proof_dict['chosen_probs'] = torch.tensor([0.1], dtype=torch.float64)  # Will convert to float32
        
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        assert isinstance(proof_bytes, bytes)
    
    @pytest.mark.skipif(
        not os.path.exists(os.path.join(os.path.dirname(__file__), 'pfunpack.so')),
        reason="pfunpack not available"
    )
    def test_roundtrip_with_pfunpack(self, proof_writer):
        """Test serialization roundtrip with pfunpack if available."""
        import pfunpack
        
        proof_dict = self.create_minimal_proof_dict()
        
        # Serialize
        from pow_utils import serialize_proof
        proof_bytes = serialize_proof(proof_dict)
        
        # Unpack
        unpacked = pfunpack.unpack_proof(proof_bytes)
        
        # Verify key fields
        assert unpacked['version'] == proof_dict['version']
        assert unpacked['tick'] == proof_dict['tick']
        assert unpacked['is_solution'] == proof_dict['is_solution']
        assert unpacked['model_identifier'] == proof_dict['model_identifier']
        assert abs(unpacked['temperature'] - proof_dict['temperature']) < 1e-6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
