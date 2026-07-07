#!/usr/bin/env python3
"""
Isolated test runner that creates all necessary mocks inline
This runs tests without needing external dependencies
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any
import subprocess

def create_minimal_test_environment():
    """Create a minimal test environment with all necessary mocks"""
    
    # Get paths
    test_dir = Path(__file__).parent
    src_dir = test_dir.parent / "src"
    
    # Create temp directory for mocks
    temp_dir = tempfile.mkdtemp(prefix="test_mocks_")
    
    print(f"Creating test mocks in {temp_dir}")
    
    # Create mock utils module
    utils_dir = Path(temp_dir) / "utils"
    utils_dir.mkdir(parents=True)
    
    # Mock uint256_arithmetics.py
    uint256_mock = utils_dir / "uint256_arithmetics.py"
    uint256_mock.write_text('''
"""Mock uint256 arithmetic functions"""

def set_compact(target_bytes):
    """Mock set_compact"""
    return 0x1d00ffff

def get_compact(nbits):
    """Mock get_compact"""
    return b"\\xff" * 32

def adjust_nbits_by_multiplier(nbits, multiplier, default_difficulty):
    """Mock adjust_nbits_by_multiplier"""
    return {
        "target_bytes": b"\\xff" * 32,
        "nbits": nbits
    }
''')
    
    # Mock pow_utils.py
    pow_utils_mock = utils_dir / "pow_utils.py"
    pow_utils_mock.write_text('''
"""Mock PoW utility functions"""

def calculate_hash(data):
    """Mock hash calculation"""
    return "0" * 64

def verify_pow(hash_value, target):
    """Mock PoW verification"""
    return True
''')
    
    # Create __init__.py
    (utils_dir / "__init__.py").touch()
    
    # Create mock chiavdf module
    chiavdf_mock = Path(temp_dir) / "chiavdf.py"
    chiavdf_mock.write_text('''
"""Mock ChiaVDF module"""
import hashlib

class MockProver:
    def __init__(self, discriminant, checkpoint_size):
        self.discriminant = discriminant
        self.checkpoint_size = checkpoint_size
        self.iterations = 0
    
    def prove(self, iterations):
        self.iterations += iterations
        return f"mock_proof_{self.iterations}".encode()

def prove_n_wesolowski(discriminant, initial_el, iterations, disc_size, checkpoint_size):
    return MockProver(discriminant, checkpoint_size)

def create_discriminant(seed, disc_size):
    return hashlib.sha256(seed).digest()[:disc_size // 8]
''')
    
    # Create mock config constants
    config_dir = Path(temp_dir) / "config"
    config_dir.mkdir()
    config_constants = config_dir / "constants.py"
    config_constants.write_text('''
"""Mock configuration constants"""
TOPK_MIN = 5
TOPK_MAX = 50
TOPP_MIN = 0.1
TOPP_MAX = 1.0
TEMP_MIN = 0.25
TEMP_MAX = 2.0
DEFAULT_TOP_K = 50
DEFAULT_TOP_P = 1.0
DEFAULT_TEMP = 1.0
''')
    (config_dir / "__init__.py").touch()
    
    return temp_dir

def run_isolated_tests():
    """Run tests with isolated mock environment"""
    
    test_dir = Path(__file__).parent
    src_dir = test_dir.parent / "src"
    
    # Create mocks
    mock_dir = create_minimal_test_environment()
    
    try:
        # Add paths to sys.path
        sys.path.insert(0, str(mock_dir))  # Mock modules first
        sys.path.insert(0, str(src_dir))    # Then src
        sys.path.insert(0, str(test_dir))   # Then tests
        
        # Set test mode
        os.environ['TEST_MODE'] = 'true'
        
        print("\nRunning tests with mock environment...")
        print("=" * 50)
        
        # Import and run specific tests that work with mocks
        import test_context
        import test_simple
        
        # Run tests using unittest
        import unittest
        
        # Create test suite
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        
        # Add test modules
        suite.addTests(loader.loadTestsFromModule(test_context))
        suite.addTests(loader.loadTestsFromModule(test_simple))
        
        # Run tests
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        
        # Print summary
        print("\n" + "=" * 50)
        print(f"Tests run: {result.testsRun}")
        print(f"Failures: {len(result.failures)}")
        print(f"Errors: {len(result.errors)}")
        print(f"Success: {result.wasSuccessful()}")
        print("=" * 50)
        
        return 0 if result.wasSuccessful() else 1
        
    finally:
        # Cleanup
        print(f"\nCleaning up mock directory: {mock_dir}")
        shutil.rmtree(mock_dir, ignore_errors=True)
        
        # Remove from sys.path
        if str(mock_dir) in sys.path:
            sys.path.remove(str(mock_dir))

def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Run isolated tests with mock dependencies"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)
    
    return run_isolated_tests()

if __name__ == "__main__":
    sys.exit(main())
