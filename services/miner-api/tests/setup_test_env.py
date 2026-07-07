#!/usr/bin/env python3
"""
Test environment setup script with automatic cleanup
Handles copying shared utilities and creating mocks for testing
"""

import os
import sys
import shutil
import tempfile
import atexit
import subprocess
import argparse
from pathlib import Path
from typing import List, Optional

# ANSI color codes
RED = '\033[0;31m'
GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
NC = '\033[0m'  # No Color


class TestEnvironmentSetup:
    """Manages test environment setup and cleanup"""
    
    def __init__(self, test_dir: Path):
        self.test_dir = test_dir
        self.src_dir = test_dir.parent / "src"
        self.project_root = test_dir.parent.parent.parent
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_env_", dir=test_dir))
        self.created_dirs: List[Path] = []
        self.created_files: List[Path] = []
        
        # Register cleanup on exit
        atexit.register(self.cleanup)
    
    def log(self, message: str, color: str = ""):
        """Print colored log message"""
        print(f"{color}{message}{NC}")
    
    def cleanup(self):
        """Clean up all created test artifacts"""
        self.log("Cleaning up test environment...", YELLOW)
        
        # Remove temporary directory
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        # Remove created directories (marked with .test_marker)
        for dir_path in self.created_dirs:
            marker = dir_path / ".test_marker"
            if marker.exists() and dir_path.exists():
                shutil.rmtree(dir_path, ignore_errors=True)
                self.log(f"  ✓ Removed {dir_path.name}")
        
        # Remove created files
        for file_path in self.created_files:
            if file_path.exists():
                file_path.unlink()
                self.log(f"  ✓ Removed {file_path.name}")
        
        # Clean Python cache
        for cache_dir in self.test_dir.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
        for cache_dir in self.src_dir.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
        
        self.log("Cleanup complete", GREEN)
    
    def copy_shared_utils(self):
        """Copy shared utilities to src directory"""
        self.log("Copying shared utilities...", YELLOW)
        
        # Create utils directory
        utils_dir = self.src_dir / "utils"
        utils_dir.mkdir(parents=True, exist_ok=True)
        (utils_dir / ".test_marker").touch()
        self.created_dirs.append(utils_dir)
        
        # Copy pow-utils
        pow_utils_src = self.project_root / "shared-utils" / "pow-utils"
        if pow_utils_src.exists():
            for file_path in pow_utils_src.glob("*.py"):
                shutil.copy2(file_path, utils_dir)
            self.log("  ✓ Copied pow-utils")
        else:
            self.log(f"  ✗ pow-utils not found at {pow_utils_src}", RED)
        
        # Create __init__.py
        (utils_dir / "__init__.py").touch()
    
    def generate_flatbuffers(self):
        """Generate Flatbuffers Python bindings"""
        self.log("Generating Flatbuffers Python bindings...", YELLOW)
        
        # Check if flatc is available
        try:
            subprocess.run(["flatc", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.log("  ✗ flatc not found. Skipping Flatbuffers generation.", RED)
            return False
        
        # Create proof directory
        proof_dir = self.src_dir / "proof"
        proof_dir.mkdir(exist_ok=True)
        (proof_dir / ".test_marker").touch()
        self.created_dirs.append(proof_dir)
        
        # Generate Python bindings
        fb_schemas_dir = self.project_root / "shared-utils" / "fb-schemas"
        if fb_schemas_dir.exists():
            for fbs_file in fb_schemas_dir.glob("*.fbs"):
                try:
                    subprocess.run(
                        ["flatc", "--python", "-o", str(self.src_dir), str(fbs_file)],
                        capture_output=True,
                        check=True
                    )
                    self.log(f"  ✓ Generated bindings for {fbs_file.name}")
                except subprocess.CalledProcessError as e:
                    self.log(f"  ✗ Failed to generate {fbs_file.name}: {e}", RED)
        else:
            self.log(f"  ✗ fb-schemas not found at {fb_schemas_dir}", RED)
            return False
        
        # Create __init__.py
        (proof_dir / "__init__.py").touch()
        self.log("Flatbuffers bindings generated", GREEN)
        return True
    
    def create_chiavdf_mock(self):
        """Create a mock ChiaVDF module for testing"""
        self.log("Creating ChiaVDF mock module...", YELLOW)
        
        mock_content = '''"""Mock ChiaVDF module for testing"""
import hashlib
import base64
import time
from typing import Optional, Tuple

class MockProver:
    """Mock VDF prover for testing"""
    
    def __init__(self, discriminant: bytes, checkpoint_size: int):
        self.discriminant = discriminant
        self.checkpoint_size = checkpoint_size
        self.iterations = 0
        self.proofs_generated = []
    
    def prove(self, iterations: int) -> bytes:
        """Mock prove method - returns deterministic fake proof"""
        self.iterations += iterations
        # Create deterministic fake proof based on inputs
        proof_data = f"mock_proof_{self.iterations}_{self.checkpoint_size}"
        proof_bytes = proof_data.encode()
        self.proofs_generated.append((iterations, proof_bytes))
        return proof_bytes
    
    def verify(self, proof: bytes, iterations: int) -> bool:
        """Mock verify method"""
        return True

def prove_n_wesolowski(discriminant: bytes, initial_el: bytes, 
                       iterations: int, disc_size: int, 
                       checkpoint_size: int) -> MockProver:
    """Mock prove_n_wesolowski - returns mock prover"""
    return MockProver(discriminant, checkpoint_size)

def create_discriminant(seed: bytes, disc_size: int) -> bytes:
    """Mock create_discriminant - returns hash-based discriminant"""
    return hashlib.sha256(seed).digest()[:disc_size // 8]

def verify_wesolowski(discriminant: bytes, initial_el: bytes, 
                      proof: bytes, iterations: int, 
                      disc_size: int) -> bool:
    """Mock verify_wesolowski - always returns True in test mode"""
    return True

# Test mode marker
__test_module__ = True
'''
        
        chiavdf_path = self.src_dir / "chiavdf.py"
        chiavdf_path.write_text(mock_content)
        self.created_files.append(chiavdf_path)
        
        self.log("ChiaVDF mock created", GREEN)
    
    def create_config_mock(self):
        """Create mock config module if needed"""
        self.log("Creating config mock module...", YELLOW)
        
        config_dir = self.src_dir / "config"
        config_dir.mkdir(exist_ok=True)
        
        # Create mock constants
        constants_content = '''"""Mock constants for testing"""
# Add any missing constants here that are imported by components
DEFAULT_DIFFICULTY = 1000000
BASE_NBITS = 536990216
DEFAULT_VERSION = 3

TOPK_MIN = 5
TOPK_MAX = 50
TOPP_MIN = 0.1
TOPP_MAX = 1.0
TEMP_MIN = 0.25
TEMP_MAX = 2.0

DEFAULT_TOP_K = 50
DEFAULT_TOP_P = 1.0
DEFAULT_TEMP = 1.0
'''
        
        constants_path = config_dir / "constants.py"
        if not constants_path.exists():
            constants_path.write_text(constants_content)
            self.created_files.append(constants_path)
        
        (config_dir / "__init__.py").touch()
        self.log("Config mock created", GREEN)
    
    def install_python_deps(self):
        """Install Python test dependencies"""
        self.log("Installing Python test dependencies...", YELLOW)
        
        requirements = [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "pytest-cov>=4.0.0",
            "aiohttp>=3.8.0",
            "pyzmq>=25.0.0",
            "flatbuffers>=23.0.0",
            "numpy>=1.20.0",
        ]
        
        # Write requirements to temp file
        req_file = self.temp_dir / "test_requirements.txt"
        req_file.write_text("\n".join(requirements))
        
        # Install dependencies
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)],
                check=True
            )
            self.log("Python dependencies installed", GREEN)
        except subprocess.CalledProcessError as e:
            self.log(f"Failed to install dependencies: {e}", RED)
            return False
        
        return True
    
    def create_test_config(self):
        """Create pytest configuration files"""
        self.log("Creating test configuration...", YELLOW)
        
        # Create pytest.ini
        pytest_ini = self.test_dir / "pytest.ini"
        pytest_ini_content = '''[pytest]
testpaths = .
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
addopts = 
    -v
    --tb=short
    --color=yes
filterwarnings =
    ignore::DeprecationWarning
'''
        pytest_ini.write_text(pytest_ini_content)
        self.created_files.append(pytest_ini)
        
        # Create/update conftest.py
        conftest_py = self.test_dir / "conftest.py"
        conftest_content = '''"""Test configuration and fixtures"""
import sys
import os
from pathlib import Path

# Add src directory to path
test_dir = Path(__file__).parent
src_dir = test_dir.parent / "src"
sys.path.insert(0, str(src_dir))

# Add project root for shared utils access
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

# Environment variable to indicate test mode
os.environ['TEST_MODE'] = 'true'

# Reduce log verbosity during tests
import logging
logging.getLogger().setLevel(logging.WARNING)

# Fixtures for testing
import pytest

@pytest.fixture
def mock_context():
    """Provide a mock LockFreeContext"""
    from components.context import LockFreeContext
    return LockFreeContext("0" * 64, "ffff" * 16)
'''
        conftest_py.write_text(conftest_content)
        self.created_files.append(conftest_py)
        
        self.log("Test configuration created", GREEN)
    
    def setup(self, skip_chiavdf: bool = False, 
             skip_flatbuffers: bool = False) -> bool:
        """Run full setup process"""
        print("=" * 50)
        print("Setting up test environment")
        print("=" * 50)
        
        try:
            # Setup steps
            self.copy_shared_utils()
            
            if not skip_flatbuffers:
                self.generate_flatbuffers()
            
            if not skip_chiavdf:
                self.create_chiavdf_mock()
            
            self.create_config_mock()
            
            if not self.install_python_deps():
                return False
            
            self.create_test_config()
            
            print()
            self.log("=" * 50, GREEN)
            self.log("Test environment setup complete!", GREEN)
            self.log("=" * 50, GREEN)
            print()
            print("You can now run tests with:")
            print(f"  cd {self.test_dir}")
            print("  python3 -m pytest")
            print()
            print("Environment will be cleaned up automatically on exit.")
            
            return True
            
        except Exception as e:
            self.log(f"Setup failed: {e}", RED)
            return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Setup test environment for miner-api"
    )
    parser.add_argument(
        "--skip-chiavdf",
        action="store_true",
        help="Skip ChiaVDF mock creation"
    )
    parser.add_argument(
        "--skip-flatbuffers", 
        action="store_true",
        help="Skip Flatbuffers generation"
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only run cleanup"
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="Run tests immediately after setup"
    )
    
    args = parser.parse_args()
    
    # Get test directory
    test_dir = Path(__file__).parent
    
    # Create setup instance
    setup = TestEnvironmentSetup(test_dir)
    
    if args.cleanup_only:
        setup.cleanup()
        return 0
    
    # Run setup
    if not setup.setup(
        skip_chiavdf=args.skip_chiavdf,
        skip_flatbuffers=args.skip_flatbuffers
    ):
        return 1
    
    # Optionally run tests
    if args.run_tests:
        print()
        setup.log("Running tests...", YELLOW)
        result = subprocess.run(
            [sys.executable, "-m", "pytest"],
            cwd=test_dir
        )
        return result.returncode
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
