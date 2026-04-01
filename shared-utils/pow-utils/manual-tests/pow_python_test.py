#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Test script to verify equivalency between Python and C++ PoW implementations.
Run this alongside the C++ tests to ensure matching outputs.
"""

import torch
import hashlib
import json
from typing import List, Tuple
import sys

# Import your actual PoW module here
from pow_utils import _tok_le_bytes, _u32le, _str_bytes, _build_msg, _digest_to_u, hex_to_bytes_tensor

def run_tests():
    print("Python PoW Utilities Test Suite")
    print("===============================\n")
    
    # Test 1: Byte conversions
    print("Test 1: Byte Conversions")
    
    # Test hex conversion
    hex_str = "0123456789abcdef"
    bytes_tensor = hex_to_bytes_tensor(hex_str)
    print(f"  hex_to_bytes: {bytes_tensor.tolist()}")
    assert bytes_tensor[0] == 0x01
    assert bytes_tensor[7] == 0xef
    
    # Test token to bytes
    tokens = torch.tensor([[1234, 5678]], dtype=torch.int64)
    ctx_bytes = _tok_le_bytes(tokens)
    print(f"  tok_le_bytes shape: {ctx_bytes.shape}")
    print(f"  tok_le_bytes hex: {ctx_bytes.cpu().numpy().tobytes().hex()}")
    
    # Test u32le
    value = torch.tensor(0x12345678, dtype=torch.uint32)
    u32_bytes = _u32le(value.view(1))
    print(f"  u32le: {u32_bytes.cpu().numpy().tobytes().hex()}")
    assert u32_bytes[0, 0] == 0x78  # LSB
    assert u32_bytes[0, 3] == 0x12  # MSB
    
    # Test digest_to_u
    digest = torch.tensor([[0x00, 0x00, 0x00, 0x80]], dtype=torch.uint8)
    u = _digest_to_u(digest)
    expected_u = 2147483648.0 / 4294967296.0
    print(f"  digest_to_u: {u.item():.10f} (expected: {expected_u:.10f})")
    assert abs(u.item() - expected_u) < 1e-7
    
    # Test 2: SHA-256
    print("\nTest 2: SHA-256 Hashing")
    msg = b"abc"
    digest = hashlib.sha256(msg).digest()
    print(f"  SHA256('abc'): {digest.hex()}")
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert digest.hex() == expected
    
    # Test 3: Full token sampling process
    print("\nTest 3: Token Sampling Process")
    
    # Setup parameters
    h_b = hex_to_bytes_tensor("0" * 63 + "1")
    v = hex_to_bytes_tensor("0" * 63 + "2")
    tick = 100
    step = 42
    
    # Context tokens
    context_tokens = torch.tensor([[1234, 5678]], dtype=torch.int64)
    ctx_bytes = _tok_le_bytes(context_tokens)
    
    # Build message components
    j4 = _u32le(torch.tensor([step], dtype=torch.int32).view(-1, 1))
    T8_32 = _u32le(torch.tensor([tick], dtype=torch.uint32))
    T8 = torch.zeros(1, 8, dtype=torch.uint8)
    T8[0, :4] = T8_32[0]
    
    precision = _str_bytes("fp16", batch_size=1)
    
    # Build complete message
    msg = _build_msg(h_b, v, T8, j4, ctx_bytes, precision)
    print(f"  Message length: {msg.shape[1]} bytes")
    print(f"  Message hex (first 64 bytes): {msg.cpu().numpy().tobytes()[:64].hex()}")
    
    # Compute hash
    msg_bytes = msg[0].cpu().numpy().tobytes()
    digest = hashlib.sha256(msg_bytes).digest()
    digest_tensor = torch.tensor(list(digest), dtype=torch.uint8).unsqueeze(0)
    print(f"  Digest: {digest.hex()}")
    
    # Convert to U value
    u = _digest_to_u(digest_tensor)
    print(f"  U value: {u.item():.10f}")
    
    # Sample from CDF
    cdf = torch.tensor([0.1, 0.3, 0.6, 0.8, 0.9, 0.95, 0.99, 1.0])
    token_id = torch.searchsorted(cdf, u)
    print(f"  Sampled token ID: {token_id.item()}")
    
    # Test 4: Generate test vectors for C++
    print("\nTest 4: Generating Test Vectors")
    test_vectors = []
    
    for i in range(3):
        context = torch.tensor([[100 + i, 200 + i, 300 + i]], dtype=torch.int64)
        step_val = 10 + i
        
        ctx_bytes = _tok_le_bytes(context)
        j4 = _u32le(torch.tensor([step_val], dtype=torch.int32).view(-1, 1))
        msg = _build_msg(h_b, v, T8, j4, ctx_bytes, precision)
        
        msg_bytes = msg[0].cpu().numpy().tobytes()
        digest = hashlib.sha256(msg_bytes).digest()
        digest_tensor = torch.tensor(list(digest), dtype=torch.uint8).unsqueeze(0)
        u = _digest_to_u(digest_tensor)
        token_id = torch.searchsorted(cdf, u)
        
        vector = {
            "context": context[0].tolist(),
            "step": step_val,
            "msg_hex": msg_bytes.hex(),
            "digest_hex": digest.hex(),
            "u_value": float(u.item()),
            "token_id": int(token_id.item())
        }
        test_vectors.append(vector)
        
    # Save test vectors
    with open("test_vectors.json", "w") as f:
        json.dump(test_vectors, f, indent=2)
    print("  Test vectors saved to test_vectors.json")
    
    print("\nAll Python tests completed!")
    print("\nTo verify equivalency:")
    print("1. Run the C++ test program")
    print("2. Compare the hex outputs for messages and digests")
    print("3. Verify U values match to at least 6 decimal places")
    print("4. Ensure token IDs are identical")

if __name__ == "__main__":
    run_tests()