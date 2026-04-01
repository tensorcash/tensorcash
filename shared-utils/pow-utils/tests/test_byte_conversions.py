# SPDX-License-Identifier: Apache-2.0
"""Test byte conversion functions for cross-language parity."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import numpy as np
from pow_utils import (
    hex_to_bytes_tensor,
    _tok_le_bytes,
    _u32le,
    _str_bytes,
    _digest_to_u
)


class TestByteConversions:
    """Test byte conversion functions against known vectors."""
    
    def test_hex_to_bytes_tensor(self):
        """Test hex string to bytes tensor conversion."""
        # Test basic conversion
        hex_str = "0123456789abcdef"
        result = hex_to_bytes_tensor(hex_str)
        expected = torch.tensor([0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef], dtype=torch.uint8)
        assert torch.equal(result, expected)
        
        # Test with uppercase
        hex_str = "FEDCBA9876543210"
        result = hex_to_bytes_tensor(hex_str)
        expected = torch.tensor([0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54, 0x32, 0x10], dtype=torch.uint8)
        assert torch.equal(result, expected)
        
        # Test with zeros (not empty - empty causes frombuffer error)
        result = hex_to_bytes_tensor("0000")
        assert result.shape == (2,)
        assert torch.all(result == 0)
        
        # Test single byte
        result = hex_to_bytes_tensor("ff")
        expected = torch.tensor([0xff], dtype=torch.uint8)
        assert torch.equal(result, expected)
    
    def test_tok_le_bytes(self):
        """Test token to little-endian bytes conversion - expects batched input."""
        # Test single token with batch dimension
        tokens = torch.tensor([[0x0123456789ABCDEF]], dtype=torch.int64)  # Shape (1, 1)
        result = _tok_le_bytes(tokens)
        # Little-endian: LSB first - output is (B, L*8) = (1, 8)
        assert result.shape == (1, 8)
        # Check the bytes are correct (little-endian)
        expected_bytes = [0xEF, 0xCD, 0xAB, 0x89, 0x67, 0x45, 0x23, 0x01]
        assert all(result[0, i] == expected_bytes[i] for i in range(8))
        
        # Test multiple tokens in batch (use values that fit in signed int64)
        tokens = torch.tensor([[0x0123456789ABCDEF, 0x7EDCBA9876543210]], dtype=torch.int64)  # Shape (1, 2)
        result = _tok_le_bytes(tokens)
        assert result.shape == (1, 16)  # 2 tokens * 8 bytes
        
        # Test batch of sequences (use values that fit in signed int64)
        tokens = torch.tensor([
            [0x0123456789ABCDEF],
            [0x7EDCBA9876543210]
        ], dtype=torch.int64)  # Shape (2, 1)
        result = _tok_le_bytes(tokens)
        assert result.shape == (2, 8)
    
    def test_u32le(self):
        """Test uint32 to little-endian bytes conversion - expects batched input."""
        # Test single value with batch dimension
        value = torch.tensor([0x12345678], dtype=torch.int32)  # Shape (1,)
        result = _u32le(value)
        assert result.shape == (1, 4)
        expected = torch.tensor([[0x78, 0x56, 0x34, 0x12]], dtype=torch.uint8)
        assert torch.equal(result, expected)
        
        # Test batch of values (use values that fit in signed int32)
        values = torch.tensor([0x12345678, 0x7BCDEF00], dtype=torch.int32)  # Shape (2,)
        result = _u32le(values)
        assert result.shape == (2, 4)
        
        # Test zero
        result = _u32le(torch.tensor([0], dtype=torch.int32))
        expected = torch.zeros(1, 4, dtype=torch.uint8)
        assert torch.equal(result, expected)
        
        # Test max signed int32
        result = _u32le(torch.tensor([0x7FFFFFFF], dtype=torch.int32))
        expected = torch.tensor([[0xFF, 0xFF, 0xFF, 0x7F]], dtype=torch.uint8)
        assert torch.equal(result, expected)
    
    def test_str_bytes(self):
        """Test string to bytes conversion - now requires batch_size."""
        # Test ASCII string with batch size
        text = "hello"
        batch_size = 2
        result = _str_bytes(text, batch_size=batch_size)
        assert result.shape == (batch_size, len(text))
        expected = torch.tensor([[104, 101, 108, 108, 111],
                                  [104, 101, 108, 108, 111]], dtype=torch.uint8)
        assert torch.equal(result, expected)
        
        # Test empty string
        result = _str_bytes("", batch_size=1)
        assert result.shape == (1, 0)
        
        # Test string with numbers
        text = "test123"
        result = _str_bytes(text, batch_size=3)
        assert result.shape == (3, 7)
        # All rows should be identical
        for i in range(3):
            assert torch.equal(result[i], result[0])
        
        # Test with different device
        text = "cuda"
        result = _str_bytes(text, batch_size=1, device=torch.device('cpu'))
        assert result.shape == (1, 4)
        assert result.device.type == 'cpu'
    
    def test_digest_to_u(self):
        """Test digest to uniform float conversion - expects batched input."""
        # Test known digest values with batch dimension
        # All zeros -> 0.0
        digest = torch.zeros(1, 32, dtype=torch.uint8)  # Shape (1, 32)
        result = _digest_to_u(digest)
        assert result[0] == 0.0
        
        # 0x80000000 in little-endian -> 0.5
        digest = torch.zeros(1, 32, dtype=torch.uint8)
        digest[0, 3] = 0x80  # Set bit 31
        result = _digest_to_u(digest)
        expected = 2147483648.0 / 4294967296.0  # 0.5
        assert abs(result[0] - expected) < 1e-9
        
        # All ones in first 4 bytes -> almost 1.0
        digest = torch.zeros(1, 32, dtype=torch.uint8)
        digest[0, :4] = 0xFF
        result = _digest_to_u(digest)
        expected = 4294967295.0 / 4294967296.0
        assert abs(result[0] - expected) < 1e-9
        
        # Test batch of digests
        batch_size = 3
        digest = torch.zeros(batch_size, 32, dtype=torch.uint8)
        digest[0, 0] = 0x12
        digest[1, 0] = 0x34
        digest[2, 0] = 0x56
        result = _digest_to_u(digest)
        assert result.shape == (batch_size,)
        
        # Test that results are always in [0, 1)
        for _ in range(5):
            digest = torch.randint(0, 256, (2, 32), dtype=torch.uint8)
            result = _digest_to_u(digest)
            assert torch.all((result >= 0.0) & (result < 1.0))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])