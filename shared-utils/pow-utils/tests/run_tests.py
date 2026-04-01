#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run all pow-utils tests with coverage reporting."""

import sys
import os
import subprocess

def main():
    """Run pytest with coverage for pow-utils."""
    test_dir = os.path.dirname(os.path.abspath(__file__))
    pow_utils_dir = os.path.dirname(test_dir)
    
    # Add parent dir to path for imports
    sys.path.insert(0, pow_utils_dir)
    
    # Run pytest with coverage
    cmd = [
        sys.executable, "-m", "pytest",
        test_dir,
        "-v",
        "--tb=short",
        f"--cov={pow_utils_dir}",
        "--cov-report=term-missing",
        "--cov-report=html:coverage_html",
        "--disable-warnings",
        "--maxfail=5"
    ]
    
    print(f"Running tests from: {test_dir}")
    print(f"Coverage for: {pow_utils_dir}")
    print(f"Command: {' '.join(cmd)}")
    print("-" * 60)
    
    result = subprocess.run(cmd)
    
    if result.returncode == 0:
        print("\n✅ All tests passed!")
        print("Coverage report saved to coverage_html/index.html")
    else:
        print(f"\n❌ Tests failed with exit code {result.returncode}")
    
    return result.returncode

if __name__ == "__main__":
    sys.exit(main())