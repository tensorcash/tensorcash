# SPDX-License-Identifier: Apache-2.0
"""Test SHA-256 and message building functions for cross-language parity."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import hashlib
from pow_utils import (
    _build_msg,
    sha256_many,
    hex_to_bytes_tensor,
    _tok_le_bytes,
    _u32le,
    _str_bytes
)


class TestSHA256AndMessageBuilding:
    """Test SHA-256 and message construction for deterministic hashing."""
    
    def test_build_msg_basic(self):
        """Test basic message building with all components."""
        # Create test inputs matching the new signature:
        # _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        
        # header_prefix: (76,) ByteTensor for block header or (32,) for legacy
        header_prefix = hex_to_bytes_tensor("00" * 76)  # 76 zero bytes
        
        # v: (32,) ByteTensor - VDF
        v = hex_to_bytes_tensor("ff" * 32)  # 32 0xff bytes
        
        # T8: (B, 8) ByteTensor - tick as 8 bytes
        tick = 1000
        T8 = torch.zeros(1, 8, dtype=torch.uint8)  # Batch size 1
        T8[0, :4] = _u32le(torch.tensor([tick]))[0]  # First 4 bytes are tick
        
        # j4: (B, 4) ByteTensor - step as 4 bytes
        step = 42
        j4 = _u32le(torch.tensor([step]))  # Shape (1, 4)
        
        # ctx_bytes: (B, L*8) ByteTensor - context tokens as bytes
        context_tokens = torch.tensor([[100, 200, 300]], dtype=torch.int64)  # Shape (1, 3)
        ctx_bytes = _tok_le_bytes(context_tokens)  # Shape (1, 24)
        
        # precision: (B, len) ByteTensor - compute precision string
        precision = _str_bytes("fp16", batch_size=1)  # Shape (1, 4)
        
        # Build message
        msg = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        
        # Verify message structure
        assert isinstance(msg, torch.Tensor)
        assert msg.dtype == torch.uint8
        assert msg.ndim == 2  # Should be (B, total_length)
        assert msg.shape[0] == 1  # Batch size 1
        
        # Expected size:
        # header_prefix: 76 bytes
        # v: 32 bytes
        # T8: 8 bytes
        # j4: 4 bytes
        # ctx_bytes: 24 bytes (3 tokens * 8)
        # precision: 4 bytes
        expected_size = 76 + 32 + 8 + 4 + 24 + 4
        assert msg.shape[1] == expected_size
    
    def test_build_msg_legacy(self):
        """Test message building with legacy block hash (32 bytes)."""
        # Use 32-byte header_prefix for legacy mode
        header_prefix = hex_to_bytes_tensor("ab" * 32)  # 32 bytes
        v = hex_to_bytes_tensor("cd" * 32)
        
        # Batch of 2
        batch_size = 2
        T8 = torch.zeros(8, dtype=torch.uint8)  # T8 should not have batch dimension
        j4 = _u32le(torch.tensor([10, 20]))  # Shape (2, 4)
        ctx_bytes = _tok_le_bytes(torch.tensor([[1], [2]], dtype=torch.int64))  # Shape (2, 8)
        precision = _str_bytes("fp32", batch_size=batch_size)
        
        msg = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        
        assert msg.shape[0] == batch_size
        # 32 + 32 + 8 + 4 + 8 + 4 = 88 bytes per message
        assert msg.shape[1] == 88
    
    def test_build_msg_determinism(self):
        """Test that same inputs produce identical messages."""
        header_prefix = hex_to_bytes_tensor("00" * 76)
        v = hex_to_bytes_tensor("ff" * 32)
        T8 = torch.zeros(8, dtype=torch.uint8)  # T8 should not have batch dimension
        j4 = _u32le(torch.tensor([100]))
        ctx_bytes = _tok_le_bytes(torch.tensor([[1, 2, 3, 4, 5]], dtype=torch.int64))
        precision = _str_bytes("fp16", batch_size=1)
        
        # Build same message twice
        msg1 = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        msg2 = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        
        # Should be identical
        assert torch.equal(msg1, msg2)
    
    def test_sha256_many_single(self):
        """Test SHA-256 on single message."""
        # Known test vector: SHA-256 of "abc"
        msg_str = "abc"
        msg_bytes = _str_bytes(msg_str, batch_size=1)  # Shape (1, 3)
        
        digests = sha256_many(msg_bytes)
        
        # Expected SHA-256 of "abc"
        expected_hex = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        expected_bytes = bytes.fromhex(expected_hex)
        
        assert digests.shape == (1, 32)
        assert bytes(digests[0].cpu().numpy()) == expected_bytes
    
    def test_sha256_many_batch(self):
        """Test SHA-256 on batch of messages."""
        # Create batch of different messages
        batch_size = 3
        msgs = ["hello", "world", "test"]
        
        # Find max length and pad
        max_len = max(len(m) for m in msgs)
        
        # Create batch tensor
        batch = torch.zeros(batch_size, max_len, dtype=torch.uint8)
        for i, msg in enumerate(msgs):
            msg_bytes = torch.tensor(list(msg.encode('utf-8')), dtype=torch.uint8)
            batch[i, :len(msg_bytes)] = msg_bytes
        
        # Compute batch SHA-256
        digests = sha256_many(batch)
        
        assert digests.shape == (3, 32)
        
        # Verify each digest is different (since messages are different)
        assert not torch.equal(digests[0], digests[1])
        assert not torch.equal(digests[1], digests[2])
        assert not torch.equal(digests[0], digests[2])
    
    def test_sha256_empty_message(self):
        """Test SHA-256 of empty message."""
        msg = torch.zeros((1, 0), dtype=torch.uint8)  # Empty message
        
        digests = sha256_many(msg)
        
        # SHA-256 of empty string
        expected_hex = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        expected_bytes = bytes.fromhex(expected_hex)
        
        assert digests.shape == (1, 32)
        assert bytes(digests[0].cpu().numpy()) == expected_bytes
    
    def test_sha256_large_message(self):
        """Test SHA-256 on larger message (multiple blocks)."""
        # Create a message larger than 64 bytes (SHA-256 block size)
        msg = torch.arange(100, dtype=torch.uint8).unsqueeze(0)  # Shape (1, 100)
        
        digests = sha256_many(msg)
        
        assert digests.shape == (1, 32)
        
        # Verify determinism
        digests2 = sha256_many(msg)
        assert torch.equal(digests, digests2)
    
    def test_message_hash_complete_flow(self):
        """Test complete flow from components to hash."""
        # Setup complete context
        header_prefix = hex_to_bytes_tensor("deadbeef" * 19)  # 76 bytes
        v = hex_to_bytes_tensor("cafebabe" * 8)  # 32 bytes
        
        batch_size = 1
        tick = 12345
        T8 = torch.zeros(batch_size, 8, dtype=torch.uint8)
        T8[0, :4] = _u32le(torch.tensor([tick]))[0]
        
        step = 5
        j4 = _u32le(torch.tensor([step]))
        
        context_tokens = torch.tensor([[10, 20, 30, 40, 50]], dtype=torch.int64)
        ctx_bytes = _tok_le_bytes(context_tokens)
        
        precision = _str_bytes("fp16", batch_size=batch_size)
        
        # Build message
        msg = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        
        # Hash it
        digest = sha256_many(msg)
        
        # Verify properties
        assert digest.shape == (1, 32)
        assert digest.dtype == torch.uint8
        
        # Build same message again and verify same hash
        msg2 = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision)
        digest2 = sha256_many(msg2)
        
        assert torch.equal(digest, digest2), "Same input must produce same hash"
        
        # Change one component and verify different hash
        j4_new = _u32le(torch.tensor([step + 1]))
        msg3 = _build_msg(header_prefix, v, T8, j4_new, ctx_bytes, precision)
        digest3 = sha256_many(msg3)
        
        assert not torch.equal(digest, digest3), "Different input must produce different hash"
    
    def test_compute_precision_included_in_hash(self):
        """Verify that compute_precision is part of the hash input."""
        header_prefix = hex_to_bytes_tensor("00" * 76)
        v = hex_to_bytes_tensor("00" * 32)
        
        batch_size = 1
        T8 = torch.zeros(batch_size, 8, dtype=torch.uint8)
        j4 = _u32le(torch.tensor([1]))
        ctx_bytes = _tok_le_bytes(torch.tensor([[1, 2, 3]], dtype=torch.int64))
        
        # Build with different compute precisions
        precision_fp16 = _str_bytes("fp16", batch_size=batch_size)
        precision_fp32 = _str_bytes("fp32", batch_size=batch_size)
        
        msg_fp16 = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision_fp16)
        msg_fp32 = _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision_fp32)
        
        # Hash both
        digest_fp16 = sha256_many(msg_fp16)
        digest_fp32 = sha256_many(msg_fp32)
        
        # Must be different due to different compute_precision
        assert not torch.equal(digest_fp16, digest_fp32), \
            "compute_precision must affect the hash"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])