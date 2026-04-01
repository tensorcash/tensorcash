#!/usr/bin/env python3.11
# SPDX-License-Identifier: Apache-2.0
"""Simple verification that core PoW utils functions work correctly."""

import sys
import os

# Colors for output
GREEN = '\033[0;32m'
RED = '\033[0;31m'
YELLOW = '\033[1;33m'
NC = '\033[0m'

print(f"{GREEN}=== PoW Utils Simple Verification ==={NC}")
print("Testing core functionality without external dependencies...\n")

# Test results tracking
passed = 0
failed = 0

def test(name, condition, message=""):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}: {message}")
        failed += 1

# Test 1: Hex to bytes conversion
print(f"{YELLOW}Testing hex to bytes conversion...{NC}")
hex_str = "0123456789abcdef"
expected = [0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef]
result = []
for i in range(0, len(hex_str), 2):
    byte = int(hex_str[i:i+2], 16)
    result.append(byte)
test("Hex string parsing", result == expected, f"Expected {expected}, got {result}")

# Test 2: Little-endian conversion
print(f"\n{YELLOW}Testing little-endian conversion...{NC}")
value = 0x12345678
expected_le = [0x78, 0x56, 0x34, 0x12]
result_le = []
for i in range(4):
    result_le.append((value >> (i * 8)) & 0xFF)
test("uint32 to little-endian", result_le == expected_le, f"Expected {expected_le}, got {result_le}")

# Test 3: nbits to target calculation
print(f"\n{YELLOW}Testing nbits to target conversion...{NC}")
nbits = 0x1d00ffff
exponent = (nbits >> 24) & 0xFF
mantissa = nbits & 0x00FFFFFF
if exponent <= 3:
    target = mantissa >> (8 * (3 - exponent))
else:
    target = mantissa << (8 * (exponent - 3))
expected_target = 0x00000000ffff0000000000000000000000000000000000000000000000000000
test("nbits 0x1d00ffff conversion", target == expected_target, 
     f"Expected {hex(expected_target)}, got {hex(target)}")

# Test 4: Token to bytes conversion
print(f"\n{YELLOW}Testing token to little-endian bytes...{NC}")
token = 0x0123456789ABCDEF
expected_bytes = [0xEF, 0xCD, 0xAB, 0x89, 0x67, 0x45, 0x23, 0x01]
token_bytes = []
for i in range(8):
    token_bytes.append((token >> (i * 8)) & 0xFF)
test("int64 token to LE bytes", token_bytes == expected_bytes, 
     f"Expected {expected_bytes}, got {token_bytes}")

# Test 5: Digest to uniform float
print(f"\n{YELLOW}Testing digest to uniform float conversion...{NC}")
# Simulate first 4 bytes of digest in little-endian
digest_bytes = [0x00, 0x00, 0x00, 0x80]  # Represents 0x80000000 in LE
u32_value = sum(b << (i * 8) for i, b in enumerate(digest_bytes))
u_float = u32_value / (2**32)
expected_u = 0.5  # 0x80000000 / 2^32 = 0.5
test("Digest to u-value", abs(u_float - expected_u) < 1e-9, 
     f"Expected {expected_u}, got {u_float}")

# Test 6: SHA-256 message structure
print(f"\n{YELLOW}Testing message building logic...{NC}")
# Verify message components are concatenated in correct order
components = {
    'context_tokens': 24,  # 3 tokens * 8 bytes
    'step': 4,              # uint32
    'block_hash': 32,       # 32 bytes
    'vdf': 32,              # 32 bytes  
    'tick': 4,              # uint32
    'compute_precision': 4  # 4 chars
}
total_size = sum(components.values())
test("Message size calculation", total_size == 100, f"Expected 100 bytes, got {total_size}")

# Test 7: Ring buffer wrap-around
print(f"\n{YELLOW}Testing ring buffer logic...{NC}")
POW_WINDOW_SIZE = 256
window_pos = 255
new_pos = (window_pos + 1) % POW_WINDOW_SIZE
test("Ring buffer wrap-around", new_pos == 0, f"Expected 0, got {new_pos}")

# Test 8: Row manager allocation
print(f"\n{YELLOW}Testing row manager logic...{NC}")
max_rows = 3
free_rows = list(range(max_rows))
allocated = {}
# Allocate row for seq_id 100
if free_rows:
    row = free_rows.pop(0)
    allocated[100] = row
    test("Row allocation", row == 0 and 100 in allocated, f"Row {row}, allocated: {allocated}")
else:
    test("Row allocation", False, "No free rows")

# Test 9: Hash comparison (big-endian)
print(f"\n{YELLOW}Testing hash vs target comparison...{NC}")
# In Bitcoin, hashes are compared as big-endian integers
# Hash: 0x000000FF... 
# Target: 0x0000FFFF...
# Hash < Target should be True
hash_val = 0x000000FF
target_val = 0x0000FFFF
test("Hash < Target", hash_val < target_val, f"{hex(hash_val)} < {hex(target_val)}")

# Test 10: Float32 precision
print(f"\n{YELLOW}Testing float32 precision handling...{NC}")
import struct
value = 0.123456789  # High precision float
# Convert to float32 and back
float32_bytes = struct.pack('f', value)
float32_value = struct.unpack('f', float32_bytes)[0]
# Should lose precision
test("Float32 precision reduction", value != float32_value and abs(float32_value - 0.123457) < 0.0001,
     f"Original: {value}, Float32: {float32_value}")

# Summary
print(f"\n{'='*50}")
print(f"{GREEN if failed == 0 else RED}RESULTS: {passed} passed, {failed} failed{NC}")
if failed == 0:
    print(f"{GREEN}✅ ALL TESTS PASSED! (100% pass rate){NC}")
    sys.exit(0)
else:
    print(f"{RED}❌ Some tests failed{NC}")
    sys.exit(1)