#!/usr/bin/env python3.11
# SPDX-License-Identifier: Apache-2.0
"""
Basic test to verify ProofProcessor can be imported and used.
This is a minimal test that doesn't require full equivalence testing.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_basic_import():
    """Test that we can import the module."""
    try:
        import proof_processor
        print("✓ ProofProcessor module imports successfully")
        return True
    except ImportError as e:
        print(f"✗ Failed to import ProofProcessor: {e}")
        return False

def test_basic_instantiation():
    """Test that we can create a ProofProcessor instance."""
    try:
        import proof_processor
        proc = proof_processor.ProofProcessor()
        print("✓ ProofProcessor instance created")
        
        # Test queue size method
        size = proc.get_queue_size()
        print(f"✓ Queue size: {size}")
        return True
    except Exception as e:
        print(f"✗ Failed to instantiate ProofProcessor: {e}")
        return False

def main():
    print("=== ProofProcessor Basic Test ===")
    print()
    
    tests_passed = 0
    tests_failed = 0
    
    # Run tests
    if test_basic_import():
        tests_passed += 1
    else:
        tests_failed += 1
    
    if test_basic_instantiation():
        tests_passed += 1
    else:
        tests_failed += 1
    
    # Summary
    print()
    print("=" * 50)
    print(f"RESULTS: {tests_passed} passed, {tests_failed} failed")
    
    if tests_failed == 0:
        print("✅ ALL TESTS PASSED!")
        return 0
    else:
        print(f"❌ {tests_failed} tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())