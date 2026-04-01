#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
ACTUAL cross-language comparison that compares C++ and Python outputs.
This runs the C++ test that outputs JSON, then verifies Python produces the same results.
"""

import subprocess
import sys
import os
import json
import torch

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pow_utils import (
    hex_to_bytes_tensor,
    _tok_le_bytes,
    _u32le,
    _digest_to_u
)


def run_cpp_tests():
    """Compile and run C++ test, parse JSON output"""
    cpp_source = "test_cpp_output.cpp"
    cpp_binary = "./test_cpp_output"
    
    # Compile C++ test
    print("Compiling C++ test...")
    compile_result = subprocess.run(
        ["g++", "-std=c++17", "-O2", cpp_source, "-o", "test_cpp_output"],
        capture_output=True,
        text=True
    )
    
    if compile_result.returncode != 0:
        print(f"Compilation failed:\n{compile_result.stderr}")
        return None
    
    # Run C++ test
    print("Running C++ test...")
    run_result = subprocess.run(
        [cpp_binary],
        capture_output=True,
        text=True
    )
    
    if run_result.returncode != 0:
        print(f"C++ test failed:\n{run_result.stderr}")
        return None
    
    # Parse JSON output
    try:
        return json.loads(run_result.stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse C++ output as JSON: {e}")
        print(f"Output was:\n{run_result.stdout}")
        return None


def compare_hex_to_bytes(cpp_result):
    """Compare hex_to_bytes implementation"""
    test_data = cpp_result["hex_to_bytes"]
    input_hex = test_data["input"]
    expected = test_data["output"]
    
    # Python implementation
    py_bytes = hex_to_bytes_tensor(input_hex)
    py_hex = ''.join(f'{b:02x}' for b in py_bytes.tolist())
    
    if py_hex == expected:
        print(f"✓ hex_to_bytes: Python matches C++ ({expected})")
        return True
    else:
        print(f"✗ hex_to_bytes: Python ({py_hex}) != C++ ({expected})")
        return False


def compare_tok_le_bytes(cpp_result):
    """Compare token to little-endian bytes conversion"""
    test_data = cpp_result["tok_le_bytes"]
    token = test_data["input"]
    expected = test_data["output"]
    
    # Python implementation
    tokens = torch.tensor([[token]], dtype=torch.int64)
    py_bytes = _tok_le_bytes(tokens)[0, :8]
    py_hex = ''.join(f'{b:02x}' for b in py_bytes.tolist())
    
    if py_hex == expected:
        print(f"✓ tok_le_bytes: Python matches C++ ({expected})")
        return True
    else:
        print(f"✗ tok_le_bytes: Python ({py_hex}) != C++ ({expected})")
        return False


def compare_u32le(cpp_result):
    """Compare uint32 to little-endian bytes conversion"""
    test_data = cpp_result["u32le"]
    value = test_data["input"]
    expected = test_data["output"]
    
    # Python implementation
    tensor = torch.tensor([value], dtype=torch.uint32)
    py_bytes = _u32le(tensor)[0]
    py_hex = ''.join(f'{b:02x}' for b in py_bytes.tolist())
    
    if py_hex == expected:
        print(f"✓ u32le: Python matches C++ ({expected})")
        return True
    else:
        print(f"✗ u32le: Python ({py_hex}) != C++ ({expected})")
        return False


def compare_digest_to_u(cpp_result):
    """Compare digest to U-value conversion"""
    test_data = cpp_result["digest_to_u"]
    input_hex = test_data["input"]
    expected = test_data["output"]
    
    # Python implementation
    digest_bytes = bytes.fromhex(input_hex)
    digest = torch.tensor([list(digest_bytes)], dtype=torch.uint8)
    # Pad to 32 bytes if needed
    if digest.shape[1] < 32:
        padded = torch.zeros(1, 32, dtype=torch.uint8)
        padded[0, :digest.shape[1]] = digest[0]
        digest = padded
    
    py_value = _digest_to_u(digest)[0].item()
    
    # Compare with tolerance for floating point
    if abs(py_value - expected) < 1e-7:
        print(f"✓ digest_to_u: Python ({py_value:.10f}) matches C++ ({expected:.10f})")
        return True
    else:
        print(f"✗ digest_to_u: Python ({py_value:.10f}) != C++ ({expected:.10f})")
        return False


def main():
    print("=== ACTUAL C++/Python Cross-Language Comparison ===\n")
    
    # Run C++ tests and get results
    cpp_results = run_cpp_tests()
    if cpp_results is None:
        print("Failed to get C++ test results")
        sys.exit(1)
    
    print("\nComparing implementations...\n")
    
    # Run comparisons
    tests = [
        ("hex_to_bytes", compare_hex_to_bytes),
        ("tok_le_bytes", compare_tok_le_bytes),
        ("u32le", compare_u32le),
        ("digest_to_u", compare_digest_to_u)
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        if test_name in cpp_results:
            if test_func(cpp_results):
                passed += 1
            else:
                failed += 1
        else:
            print(f"✗ {test_name}: Missing from C++ output")
            failed += 1
    
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    
    if failed > 0:
        print("\n✗ Cross-language verification FAILED")
        sys.exit(1)
    else:
        print("\n✓ All cross-language checks passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()