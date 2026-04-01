#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# ACTUAL Cross-Language Equivalency Test
# This requires both C++ and Python to be working

set -e

echo "=== C++/Python Cross-Language Equivalency Test ==="
echo ""

# Check if C++ binary exists
if [ ! -f "../pow_test" ]; then
    echo "ERROR: C++ binary 'pow_test' not found!"
    echo "Run 'make pow_test' in the parent directory first."
    exit 1
fi

# Run C++ tests and capture output
echo "Running C++ tests..."
../pow_test > cpp_output.txt 2>&1

# Run Python tests and capture output  
echo "Running Python tests..."
python3 pow_python_test.py > python_output.txt 2>&1

# Run equivalency verification
echo "Comparing outputs..."
python3 verify_equivalency.py

echo ""
echo "If you see this message, the cross-language test passed!"
echo "Check cpp_output.txt and python_output.txt for detailed results."