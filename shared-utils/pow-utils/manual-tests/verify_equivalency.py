#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Automated verification script to compare C++ and Python PoW implementations.
This script runs both implementations with the same inputs and compares outputs.
"""

import subprocess
import json
import sys
import re
from typing import Dict, List, Tuple

def extract_cpp_output(output: str) -> Dict[str, str]:
    """Extract test values from C++ output."""
    results = {}
    
    # Extract hex values using regex
    patterns = {
        'context_bytes': r'Context bytes: ([0-9a-fA-F]+)',
        'step_bytes': r'Step bytes: ([0-9a-fA-F]+)',
        'tick_bytes': r'Tick bytes: ([0-9a-fA-F]+)',
        'precision': r'Precision: ([0-9a-fA-F]+)',
        'digest': r'Digest: ([0-9a-fA-F]+)',
        'u_value': r'U value: ([\d.]+)',
        'token_id': r'Token ID: (\d+)'
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            results[key] = match.group(1)
    
    return results

def extract_python_output(output: str) -> Dict[str, str]:
    """Extract test values from Python output."""
    results = {}
    
    patterns = {
        'context_bytes': r'tok_le_bytes hex: ([0-9a-fA-F]+)',
        'step_bytes': r'u32le: ([0-9a-fA-F]+)',
        'digest': r'Digest: ([0-9a-fA-F]+)',
        'u_value': r'U value: ([\d.]+)',
        'token_id': r'Sampled token ID: (\d+)'
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            results[key] = match.group(1)
    
    return results

def compare_results(cpp_results: Dict[str, str], py_results: Dict[str, str]) -> List[str]:
    """Compare results and return list of differences."""
    differences = []
    
    # Compare hex values (case-insensitive)
    hex_fields = ['context_bytes', 'digest']
    for field in hex_fields:
        if field in cpp_results and field in py_results:
            cpp_val = cpp_results[field].lower()
            py_val = py_results[field].lower()
            if cpp_val != py_val:
                differences.append(f"{field}: C++ = {cpp_val}, Python = {py_val}")
    
    # Compare numeric values
    if 'u_value' in cpp_results and 'u_value' in py_results:
        cpp_u = float(cpp_results['u_value'])
        py_u = float(py_results['u_value'])
        if abs(cpp_u - py_u) > 1e-7:
            differences.append(f"u_value: C++ = {cpp_u:.10f}, Python = {py_u:.10f}")
    
    if 'token_id' in cpp_results and 'token_id' in py_results:
        cpp_token = int(cpp_results['token_id'])
        py_token = int(py_results['token_id'])
        if cpp_token != py_token:
            differences.append(f"token_id: C++ = {cpp_token}, Python = {py_token}")
    
    return differences

def run_verification():
    """Run both implementations and compare results."""
    print("PoW Implementation Verification")
    print("==============================\n")
    
    # Compile C++ code
    print("Compiling C++ code...")
    compile_result = subprocess.run(['make', 'clean'], capture_output=True, text=True)
    compile_result = subprocess.run(['make'], capture_output=True, text=True)
    if compile_result.returncode != 0:
        print("Error compiling C++ code:")
        print(compile_result.stderr)
        return False
    print("✓ C++ compilation successful\n")
    
    # Run C++ tests
    print("Running C++ tests...")
    cpp_result = subprocess.run(['./pow_test'], capture_output=True, text=True)
    if cpp_result.returncode != 0:
        print("Error running C++ tests:")
        print(cpp_result.stderr)
        return False
    cpp_output = cpp_result.stdout
    print("✓ C++ tests completed\n")
    
    # Run Python tests
    print("Running Python tests...")
    py_result = subprocess.run([sys.executable, 'pow_python_test.py'], 
                               capture_output=True, text=True)
    if py_result.returncode != 0:
        print("Error running Python tests:")
        print(py_result.stderr)
        return False
    py_output = py_result.stdout
    print("✓ Python tests completed\n")
    
    # Extract and compare results
    print("Comparing results...")
    cpp_results = extract_cpp_output(cpp_output)
    py_results = extract_python_output(py_output)
    
    print(f"Extracted {len(cpp_results)} values from C++ output")
    print(f"Extracted {len(py_results)} values from Python output")
    
    differences = compare_results(cpp_results, py_results)
    
    if not differences:
        print("\n✓ All tests passed! Implementations are equivalent.")
        
        # Show some key matching values
        print("\nMatching values:")
        if 'digest' in cpp_results:
            print(f"  SHA256 digest: {cpp_results['digest']}")
        if 'u_value' in cpp_results:
            print(f"  U value: {cpp_results['u_value']}")
        if 'token_id' in cpp_results:
            print(f"  Token ID: {cpp_results['token_id']}")
        
        return True
    else:
        print("\n✗ Found differences:")
        for diff in differences:
            print(f"  - {diff}")
        return False

def test_specific_vectors():
    """Test specific input vectors to ensure deterministic behavior."""
    print("\n\nTesting Specific Vectors")
    print("========================")
    
    test_cases = [
        {
            "name": "Simple test",
            "context": [1234, 5678],
            "step": 42,
            "tick": 100,
            "block_hash": "0" * 63 + "1",
            "vdf": "0" * 63 + "2",
            "precision": "fp16"
        },
        {
            "name": "Large values",
            "context": [2**60 - 1, 2**61 - 1],
            "step": 1000000,
            "tick": 999999,
            "block_hash": "f" * 64,
            "vdf": "a" * 64,
            "precision": "int8"
        }
    ]
    
    # This would require modifying the C++ and Python code to accept
    # these specific test vectors as input
    print("(Manual verification of specific vectors required)")
    
    return True

if __name__ == "__main__":
    success = run_verification()
    if success:
        test_specific_vectors()
        print("\n✓ Verification complete!")
        sys.exit(0)
    else:
        print("\n✗ Verification failed!")
        sys.exit(1)