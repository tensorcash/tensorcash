#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Debug script to test u_value calculation with specific bytes
"""

import torch
import struct

def test_u_value_calculation():
    # Test with the same bytes that are causing the difference
    # Based on the u_value of 0.7637254, we can reverse-engineer the bytes
    
    # Method 1: Python/PyTorch style (as in original code)
    def python_style(digest_bytes):
        b0 = float(digest_bytes[0])
        b1 = float(digest_bytes[1])
        b2 = float(digest_bytes[2])
        b3 = float(digest_bytes[3])
        result = (b0 + b1 * 256.0 + b2 * 65536.0 + b3 * 16777216.0) / 4294967296.0
        return result
    
    # Method 2: C++ style (original)
    def cpp_style_original(digest_bytes):
        value = (digest_bytes[0] | 
                (digest_bytes[1] << 8) | 
                (digest_bytes[2] << 16) | 
                (digest_bytes[3] << 24))
        return float(value) / 4294967296.0
    
    # Method 3: Direct binary interpretation
    def binary_style(digest_bytes):
        # Interpret 4 bytes as little-endian uint32
        value = struct.unpack('<I', bytes(digest_bytes[:4]))[0]
        return float(value) / 4294967296.0
    
    # Test with some example bytes
    test_cases = [
        [0x83, 0x2A, 0xFE, 0xC3],  # This should give approximately 0.7637254
        [0x00, 0x00, 0x00, 0x80],  # This should give 0.5
        [0xFF, 0xFF, 0xFF, 0xFF],  # This should give ~0.9999999
    ]
    
    print("U-Value Calculation Comparison")
    print("==============================\n")
    
    for test_bytes in test_cases:
        print(f"Test bytes: {' '.join(f'{b:02x}' for b in test_bytes)}")
        
        py_result = python_style(test_bytes)
        cpp_orig = cpp_style_original(test_bytes)
        bin_result = binary_style(test_bytes)
        
        print(f"  Python style:    {py_result:.10f}")
        print(f"  C++ original:    {cpp_orig:.10f}")
        print(f"  Binary style:    {bin_result:.10f}")
        print(f"  Difference:      {abs(py_result - cpp_orig):.10e}")
        print()
    
    # Calculate what bytes would give 0.7637254
    target_u = 0.7637254
    target_uint32 = int(target_u * 4294967296)
    target_bytes = [(target_uint32 >> (i*8)) & 0xFF for i in range(4)]
    
    print(f"To get u_value = {target_u:.7f}:")
    print(f"  uint32 value: {target_uint32} (0x{target_uint32:08x})")
    print(f"  Bytes (LE): {' '.join(f'{b:02x}' for b in target_bytes)}")
    
    # Verify
    print(f"  Verification: {python_style(target_bytes):.10f}")

if __name__ == "__main__":
    test_u_value_calculation()