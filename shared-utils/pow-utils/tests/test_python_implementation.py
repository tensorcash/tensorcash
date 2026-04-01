# SPDX-License-Identifier: Apache-2.0
"""
Test Python PoW Implementation Correctness

This tests the Python implementation against known correct values and behaviors.
For actual C++/Python cross-language testing, you need to:
1. Have the C++ binary compiled and available
2. Run both with same inputs and compare outputs
3. Use the manual test scripts in manual-tests/ directory
"""

import pytest
import torch
import hashlib
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pow_utils import (
    hex_to_bytes_tensor,
    _tok_le_bytes,
    _u32le,
    _str_bytes,
    _build_msg,
    sha256_many,
    _digest_to_u,
)


class TestPythonImplementation:
    """Test that Python implementations are correct"""
    
    def test_hex_to_bytes_conversion(self):
        """Test hex to bytes conversion"""
        test_cases = [
            ("0123456789abcdef", [0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef]),
            ("deadbeef", [0xde, 0xad, 0xbe, 0xef]),
        ]
        
        for hex_str, expected in test_cases:
            result = hex_to_bytes_tensor(hex_str)
            assert result.tolist() == expected
    
    def test_token_to_bytes_little_endian(self):
        """Test token to little-endian bytes conversion"""
        tokens = torch.tensor([[1234]], dtype=torch.int64)
        result = _tok_le_bytes(tokens)
        # 1234 = 0x04D2 in hex, little-endian = D2 04 00 00 00 00 00 00
        expected = [0xd2, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        assert result[0, :8].tolist() == expected
    
    def test_u32_little_endian(self):
        """Test uint32 to little-endian bytes conversion"""
        value = torch.tensor([0x12345678], dtype=torch.uint32)
        result = _u32le(value)
        expected = [0x78, 0x56, 0x34, 0x12]  # Little-endian
        assert result[0].tolist() == expected
    
    def test_sha256_against_standard(self):
        """Test SHA-256 against Python standard library"""
        test_msg = b"Hello, World!"
        expected = hashlib.sha256(test_msg).digest()
        
        msg_tensor = torch.tensor(list(test_msg), dtype=torch.uint8).unsqueeze(0)
        result = sha256_many(msg_tensor)
        result_bytes = bytes(result[0].tolist())
        
        assert result_bytes == expected
    
    def test_digest_to_u_value_range(self):
        """Test that U-value is always in [0, 1] range"""
        # Test various digests
        for _ in range(10):
            random_digest = torch.randint(0, 256, (1, 32), dtype=torch.uint8)
            u_value = _digest_to_u(random_digest)[0].item()
            assert 0.0 <= u_value <= 1.0
    
    def test_message_building_structure(self):
        """Test message building produces correct structure"""
        header = torch.zeros(32, dtype=torch.uint8)
        vdf = torch.zeros(32, dtype=torch.uint8)
        tick = torch.tensor([0], dtype=torch.uint32)
        step = torch.tensor([0], dtype=torch.uint32)
        tokens = torch.tensor([[0, 0, 0, 0]], dtype=torch.int64)
        precision = "fp16"
        
        tokens_bytes = _tok_le_bytes(tokens)
        tick_bytes = _u32le(tick)
        step_bytes = _u32le(step)
        precision_bytes = _str_bytes(precision, 1)
        
        msg = _build_msg(header, vdf, tick_bytes, step_bytes, tokens_bytes, precision_bytes)
        
        # Message should be: header(32) + vdf(32) + tick(4) + step(4) + tokens(32) + precision(4)
        assert msg.shape == (1, 108)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])