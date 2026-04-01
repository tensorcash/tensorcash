# SPDX-License-Identifier: Apache-2.0
"""Test PowHasher for deterministic token sampling."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from pow_utils import PowHasher, hex_to_bytes_tensor, _digest_to_u


class TestPowHasher:
    """Test PowHasher token sampling and payload updates."""
    
    @pytest.fixture
    def pow_hasher(self):
        """Create a PowHasher with CPU device."""
        return PowHasher(device="cpu")
    
    def test_init(self, pow_hasher):
        """Test PowHasher initialization."""
        assert pow_hasher.device == "cpu"
        assert pow_hasher.h_b.shape == (32,)
        assert pow_hasher.v.shape == (32,)
        assert pow_hasher.target.shape == (32,)
        assert pow_hasher.target[-1] == 0xFF  # Default easy target
        assert pow_hasher.tick == 0
        assert pow_hasher.ipfs_cid is None
    
    def test_update_from_payload(self, pow_hasher):
        """Test updating parameters from payload."""
        payload = {
            "block_hash": "deadbeef" * 8,  # 32 bytes hex
            "vdf": "cafebabe" * 8,
            "tick": 12345,
            "request_id": 42,
            "target": "00000000ffff0000000000000000000000000000000000000000000000000000",
            "difficulty": 1.0,
            "header_prefix": "00" * 76,  # 76 byte header
            "compute_precision": "fp16",
            "ipfs_cid": "QmTest123",
            "model_identifier": "test-model"
        }
        
        pow_hasher.update_from_payload(payload)
        
        # Check updates
        assert pow_hasher.tick == 12345
        assert pow_hasher.request_id == 42
        assert pow_hasher.ipfs_cid == "QmTest123"
        
        # Check block hash was updated
        expected_h_b = hex_to_bytes_tensor("deadbeef" * 8, device="cpu")
        assert torch.equal(pow_hasher.h_b, expected_h_b)
        
        # Check VDF was updated
        expected_v = hex_to_bytes_tensor("cafebabe" * 8, device="cpu")
        assert torch.equal(pow_hasher.v, expected_v)
        
        # Check target was updated
        target_bytes = hex_to_bytes_tensor(payload["target"], device="cpu")
        assert torch.equal(pow_hasher.target, target_bytes)
    
    def test_update_caching(self, pow_hasher):
        """Test that unchanged values are cached."""
        payload1 = {
            "block_hash": "aa" * 32,
            "vdf": "bb" * 32,
            "tick": 100,
            "request_id": 1,
            "target": "ff" * 32,
            "difficulty": 1.0,
            "header_prefix": "00" * 76,
            "compute_precision": "fp16",
            "ipfs_cid": "Qm1",
            "model_identifier": "model1"
        }
        
        pow_hasher.update_from_payload(payload1)
        
        # Store references to tensors
        h_b_ref = pow_hasher.h_b
        v_ref = pow_hasher.v
        target_ref = pow_hasher.target
        
        # Update with same values for cached fields
        payload2 = {
            "block_hash": "aa" * 32,  # Same
            "vdf": "bb" * 32,  # Same
            "tick": 200,  # Different
            "request_id": 2,
            "target": "ff" * 32,  # Same
            "difficulty": 1.0,
            "header_prefix": "00" * 76,
            "compute_precision": "fp16",
            "ipfs_cid": "Qm2",
            "model_identifier": "model1"
        }
        
        pow_hasher.update_from_payload(payload2)
        
        # Check that cached tensors weren't recreated
        assert pow_hasher.h_b is h_b_ref
        assert pow_hasher.v is v_ref
        assert pow_hasher.target is target_ref
        
        # But tick was updated
        assert pow_hasher.tick == 200
    
    def test_sample_token_deterministic(self, pow_hasher):
        """Test deterministic token sampling."""
        # Set up hasher state
        payload = {
            "block_hash": "00" * 32,
            "vdf": "ff" * 32,
            "tick": 1000,
            "request_id": 42,
            "target": "00000000ffff0000000000000000000000000000000000000000000000000000",
            "difficulty": 1.0,
            "header_prefix": "ab" * 76,
            "compute_precision": "fp32",
            "ipfs_cid": "QmTest",
            "model_identifier": "test"
        }
        pow_hasher.update_from_payload(payload)
        pow_hasher.compute_precision = "fp32"  # Make sure precision is set
        
        # Create fake logits and compute CDF (batch_size=1, vocab_size=100)
        logits = torch.randn(1, 100)
        # Simple CDF: softmax then cumsum
        probs = torch.softmax(logits, dim=-1)
        cdf = torch.cumsum(probs, dim=-1)
        
        # Sample token with fixed context
        context = torch.tensor([[1, 2, 3]], dtype=torch.int64)  # Shape (1, 3)
        step = torch.tensor([10], dtype=torch.int32)  # Step counter
        
        # Call sample_token with correct signature (returns 3 values)
        token, u_value, digest = pow_hasher.sample_token(
            context=context,
            step=step,
            cdf=cdf
        )
        
        # Check results
        assert isinstance(token, torch.Tensor)
        assert isinstance(u_value, torch.Tensor)
        assert isinstance(digest, torch.Tensor)
        assert token.shape == (1,)  # One token per batch
        assert u_value.shape == (1,)  # One u value per batch
        assert digest.shape == (1, 32)  # One digest per batch (32 bytes)
        
        # Same inputs should give same output (deterministic)
        token2, u_value2, digest2 = pow_hasher.sample_token(
            context=context,
            step=step,
            cdf=cdf
        )
        
        assert torch.equal(token, token2)
        assert torch.equal(u_value, u_value2)
        assert torch.equal(digest, digest2)
    
    def test_target_padding(self, pow_hasher):
        """Test target padding for different lengths."""
        # Test short target (needs padding)
        payload1 = {
            "block_hash": "00" * 32,
            "vdf": "00" * 32,
            "tick": 1,
            "request_id": 1,
            "target": "ffff",  # Only 2 bytes
            "difficulty": 1.0,
            "header_prefix": "00" * 76,
            "compute_precision": "fp16",
            "ipfs_cid": "",
            "model_identifier": "test"
        }
        
        pow_hasher.update_from_payload(payload1)
        
        # Should be padded to 32 bytes with leading zeros
        assert pow_hasher.target.shape == (32,)
        assert pow_hasher.target[0] == 0  # Leading zeros
        assert pow_hasher.target[-2] == 0xff
        assert pow_hasher.target[-1] == 0xff
        
        # Test long target (needs truncation)
        payload2 = {
            "block_hash": "00" * 32,
            "vdf": "00" * 32,
            "tick": 1,
            "request_id": 1,
            "target": "aa" * 40,  # 40 bytes (too long)
            "difficulty": 1.0,
            "header_prefix": "00" * 76,
            "compute_precision": "fp16",
            "ipfs_cid": "",
            "model_identifier": "test"
        }
        
        pow_hasher.update_from_payload(payload2)
        
        # Should be truncated to last 32 bytes
        assert pow_hasher.target.shape == (32,)
        assert torch.all(pow_hasher.target == 0xaa)
    
    def test_header_prefix_extraction(self, pow_hasher):
        """Test header prefix extraction for different header types."""
        # Test with 80-byte header (Bitcoin-style)
        payload1 = {
            "block_hash": "00" * 32,
            "vdf": "00" * 32,
            "tick": 1,
            "request_id": 1,
            "target": "ff" * 32,
            "difficulty": 1.0,
            "header_prefix": "ab" * 80,  # 80 bytes
            "compute_precision": "fp16",
            "ipfs_cid": "",
            "model_identifier": "test"
        }
        
        pow_hasher.update_from_payload(payload1)
        
        # Should have full 160 bytes (80*2 hex chars = 80 bytes)
        if pow_hasher.header_prefix is not None:
            assert pow_hasher.header_prefix.shape == (80,)
            assert torch.all(pow_hasher.header_prefix == 0xab)
        
        # Test with 32-byte header (legacy mode)
        payload2 = {
            "block_hash": "00" * 32,
            "vdf": "00" * 32,
            "tick": 1,
            "request_id": 1,
            "target": "ff" * 32,
            "difficulty": 1.0,
            "header_prefix": "cd" * 32,  # 32 bytes
            "compute_precision": "fp16",
            "ipfs_cid": "",
            "model_identifier": "test"
        }
        
        pow_hasher.update_from_payload(payload2)
        
        # Should use full 32 bytes
        if pow_hasher.header_prefix is not None:
            assert pow_hasher.header_prefix.shape == (32,)
            assert torch.all(pow_hasher.header_prefix == 0xcd)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])