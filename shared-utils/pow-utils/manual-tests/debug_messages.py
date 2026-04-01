#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Debug script to print out exact byte sequences for comparison
"""

import torch
import hashlib

def _tok_le_bytes(tok_i64: torch.Tensor) -> torch.ByteTensor:
    """View an (B, L) int64 tensor as little-endian bytes."""
    t = tok_i64.contiguous()
    return t.view(torch.uint8).view(t.size(0), -1)

def _u32le(x: torch.Tensor) -> torch.ByteTensor:
    """Convert a 32-bit tensor to little-endian bytes."""
    x32 = x.reshape(-1).to(torch.uint32).contiguous()
    x8 = x32.view(torch.uint8)
    return x8.view(-1, 4)

def _str_bytes(s: str, batch_size: int, device='cpu') -> torch.ByteTensor:
    """Convert string to bytes tensor."""
    b = s.encode('utf-8')
    arr = torch.tensor(list(b), dtype=torch.uint8, device=device)
    return arr.unsqueeze(0).expand(batch_size, -1)

def hex_to_bytes_tensor(hex_str: str, device='cpu') -> torch.ByteTensor:
    """Convert hex string to ByteTensor."""
    bytes_data = bytes.fromhex(hex_str)
    return torch.tensor(list(bytes_data), dtype=torch.uint8, device=device)

# Test exact same values as C++ test
h_b = hex_to_bytes_tensor("0" * 63 + "1")
v = hex_to_bytes_tensor("0" * 63 + "2")
tick = 100
step = 42

# Context tokens
context_tokens = torch.tensor([[1234, 5678]], dtype=torch.int64)
ctx_bytes = _tok_le_bytes(context_tokens)

print("=== Python Debug Output ===")
print(f"h_b bytes: {h_b.numpy().tobytes().hex()}")
print(f"v bytes: {v.numpy().tobytes().hex()}")
print(f"Context tokens: {context_tokens[0].tolist()}")
print(f"Context bytes ({ctx_bytes.shape[1]} bytes): {ctx_bytes[0].numpy().tobytes().hex()}")

# Build message components
j4 = _u32le(torch.tensor([step], dtype=torch.int32).view(-1, 1))
print(f"Step (j4) bytes: {j4[0].numpy().tobytes().hex()}")

# IMPORTANT: T8 should be 8 bytes, but we're converting from a 32-bit tick value
T8_32 = _u32le(torch.tensor([tick], dtype=torch.uint32))
T8 = torch.zeros(1, 8, dtype=torch.uint8)
T8[0, :4] = T8_32[0]  # First 4 bytes are the tick in little-endian
# Last 4 bytes remain zeros
print(f"Tick (T8) bytes (8 bytes total): {T8[0].numpy().tobytes().hex()}")

precision = _str_bytes("fp16", batch_size=1)
print(f"Precision bytes: {precision[0].numpy().tobytes().hex()}")

# Build complete message
# Order: header_prefix (32), v (32), T8 (8), j4 (4), ctx_bytes (16), precision (4)
msg_parts = {
    "h_b (32)": h_b.numpy().tobytes().hex(),
    "v (32)": v.numpy().tobytes().hex(), 
    "T8 (8)": T8[0].numpy().tobytes().hex(),
    "j4 (4)": j4[0].numpy().tobytes().hex(),
    "ctx_bytes (16)": ctx_bytes[0].numpy().tobytes().hex(),
    "precision (4)": precision[0].numpy().tobytes().hex()
}

print("\nMessage components in order:")
total_bytes = 0
for name, hex_val in msg_parts.items():
    byte_count = len(hex_val) // 2
    total_bytes += byte_count
    print(f"  {name}: {hex_val} ({byte_count} bytes)")

# Build message
B = ctx_bytes.size(0)
msg = torch.cat([
    h_b.view(1, -1).expand(B, -1),      # 32 bytes
    v.view(1, -1).expand(B, -1),        # 32 bytes  
    T8,                                  # 8 bytes
    j4,                                  # 4 bytes
    ctx_bytes,                           # 16 bytes (2 tokens * 8 bytes each)
    precision,                           # 4 bytes
], dim=1)

print(f"\nTotal message length: {msg.shape[1]} bytes (expected: {total_bytes})")
print(f"Complete message hex:\n{msg[0].numpy().tobytes().hex()}")

# Compute hash
msg_bytes = msg[0].cpu().numpy().tobytes()
digest = hashlib.sha256(msg_bytes).digest()
print(f"\nSHA256 digest: {digest.hex()}")

# Compute U value
digest_tensor = torch.tensor(list(digest), dtype=torch.uint8)
b0 = digest_tensor[0].to(torch.float32)
b1 = digest_tensor[1].to(torch.float32) 
b2 = digest_tensor[2].to(torch.float32)
b3 = digest_tensor[3].to(torch.float32)
u = (b0 + b1 * 256 + b2 * 65536 + b3 * 16777216) / 4294967296.0
print(f"U value: {u:.10f}")

# Test CDF sampling
cdf = torch.tensor([0.1, 0.3, 0.6, 0.8, 0.9, 0.95, 0.99, 1.0])
token_id = torch.searchsorted(cdf, torch.tensor(u))
print(f"Token ID: {token_id.item()}")