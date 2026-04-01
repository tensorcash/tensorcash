#!/bin/bash
# Test environment setup script with automatic cleanup
# Copies shared utilities and optionally builds ChiaVDF for testing

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Paths
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEST_DIR="${SCRIPT_DIR}"
SRC_DIR="${SCRIPT_DIR}/../src"
PROJECT_ROOT="${SCRIPT_DIR}/../../.."
TEMP_DIR="${TEST_DIR}/.test_env_$$"

# Cleanup function
cleanup() {
    echo -e "${YELLOW}Cleaning up test environment...${NC}"
    
    # Remove temporary directories
    if [ -d "${TEMP_DIR}" ]; then
        rm -rf "${TEMP_DIR}"
    fi
    
    # Remove copied utils from src
    if [ -d "${SRC_DIR}/utils" ] && [ -f "${SRC_DIR}/utils/.test_marker" ]; then
        rm -rf "${SRC_DIR}/utils"
    fi
    
    # Remove generated proof modules
    if [ -d "${SRC_DIR}/proof" ] && [ -f "${SRC_DIR}/proof/.test_marker" ]; then
        rm -rf "${SRC_DIR}/proof"
    fi
    
    # Clean Python cache
    find "${TEST_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "${SRC_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    
    echo -e "${GREEN}Cleanup complete${NC}"
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

# Function to copy shared utilities
copy_shared_utils() {
    echo -e "${YELLOW}Copying shared utilities...${NC}"
    
    # Create utils directory in src
    mkdir -p "${SRC_DIR}/utils"
    touch "${SRC_DIR}/utils/.test_marker"  # Mark as test directory
    
    # Copy pow-utils
    if [ -d "${PROJECT_ROOT}/shared-utils/pow-utils" ]; then
        cp -r "${PROJECT_ROOT}/shared-utils/pow-utils/"* "${SRC_DIR}/utils/"
        echo "  ✓ Copied pow-utils"
    else
        echo -e "${RED}  ✗ pow-utils not found${NC}"
    fi
    
    # Create __init__.py if not exists
    touch "${SRC_DIR}/utils/__init__.py"
    
    echo -e "${GREEN}Shared utilities copied${NC}"
}

# Function to generate Flatbuffers Python bindings
generate_flatbuffers() {
    echo -e "${YELLOW}Generating Flatbuffers Python bindings...${NC}"
    
    # Check if flatc is available
    if ! command -v flatc &> /dev/null; then
        echo -e "${RED}flatc not found. Skipping Flatbuffers generation.${NC}"
        return 1
    fi
    
    # Create proof directory
    mkdir -p "${SRC_DIR}/proof"
    touch "${SRC_DIR}/proof/.test_marker"
    
    # Generate Python bindings
    local fb_schemas="${PROJECT_ROOT}/shared-utils/fb-schemas"
    if [ -d "${fb_schemas}" ]; then
        for fbs_file in "${fb_schemas}"/*.fbs; do
            if [ -f "$fbs_file" ]; then
                flatc --python -o "${SRC_DIR}" "$fbs_file"
                echo "  ✓ Generated bindings for $(basename $fbs_file)"
            fi
        done
    else
        echo -e "${RED}  ✗ fb-schemas not found${NC}"
        return 1
    fi
    
    # Create __init__.py
    touch "${SRC_DIR}/proof/__init__.py"
    
    echo -e "${GREEN}Flatbuffers bindings generated${NC}"
}

# Function to build ChiaVDF (optional, lightweight)
build_chiavdf_mock() {
    echo -e "${YELLOW}Creating ChiaVDF mock module...${NC}"
    
    # Create a mock chiavdf module for testing
    cat > "${SRC_DIR}/chiavdf.py" << 'EOF'
"""Mock ChiaVDF module for testing"""
import hashlib
import base64
import time

class MockProver:
    def __init__(self, discriminant, checkpoint_size):
        self.discriminant = discriminant
        self.checkpoint_size = checkpoint_size
        self.iterations = 0
    
    def prove(self, iterations):
        """Mock prove method - returns deterministic fake proof"""
        self.iterations += iterations
        # Create deterministic fake proof based on inputs
        proof_data = f"mock_proof_{self.iterations}_{self.checkpoint_size}"
        return proof_data.encode()

def prove_n_wesolowski(discriminant, initial_el, iterations, disc_size, checkpoint_size):
    """Mock prove_n_wesolowski - returns mock prover"""
    return MockProver(discriminant, checkpoint_size)

def create_discriminant(seed, disc_size):
    """Mock create_discriminant - returns hash-based discriminant"""
    return hashlib.sha256(seed).digest()[:disc_size // 8]

# Mark as test module
__test_module__ = True
EOF
    
    echo -e "${GREEN}ChiaVDF mock created${NC}"
}

# Function to install Python dependencies
install_python_deps() {
    echo -e "${YELLOW}Installing Python test dependencies...${NC}"
    
    # Create temporary requirements file
    cat > "${TEMP_DIR}/test_requirements.txt" << EOF
pytest>=7.0.0
pytest-asyncio>=0.21.0
pytest-cov>=4.0.0
aiohttp>=3.8.0
pyzmq>=25.0.0
flatbuffers>=23.0.0
numpy>=1.20.0
EOF
    
    # Install dependencies
    pip3 install -q -r "${TEMP_DIR}/test_requirements.txt"
    
    echo -e "${GREEN}Python dependencies installed${NC}"
}

# Function to create test configuration
create_test_config() {
    echo -e "${YELLOW}Creating test configuration...${NC}"
    
    # Create pytest.ini
    cat > "${TEST_DIR}/pytest.ini" << EOF
[pytest]
testpaths = .
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
addopts = 
    -v
    --tb=short
    --color=yes
    --cov=../src
    --cov-report=term-missing
    --cov-report=html:coverage_report
filterwarnings =
    ignore::DeprecationWarning
    ignore::PytestUnraisableExceptionWarning
EOF
    
    # Create conftest.py with path setup
    cat > "${TEST_DIR}/conftest.py" << EOF
"""Test configuration and fixtures"""
import sys
import os

# Add src directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Add project root for shared utils access
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

# Environment variable to indicate test mode
os.environ['TEST_MODE'] = 'true'

# Reduce log verbosity during tests
import logging
logging.getLogger().setLevel(logging.WARNING)
EOF
    
    echo -e "${GREEN}Test configuration created${NC}"
}

# Main setup function
main() {
    echo "========================================="
    echo "Setting up test environment"
    echo "========================================="
    
    # Parse arguments
    SKIP_CHIAVDF=false
    SKIP_FLATBUFFERS=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            --skip-chiavdf)
                SKIP_CHIAVDF=true
                shift
                ;;
            --skip-flatbuffers)
                SKIP_FLATBUFFERS=true
                shift
                ;;
            --help)
                echo "Usage: $0 [options]"
                echo "Options:"
                echo "  --skip-chiavdf     Skip ChiaVDF mock creation"
                echo "  --skip-flatbuffers Skip Flatbuffers generation"
                echo "  --help            Show this help message"
                exit 0
                ;;
            *)
                echo "Unknown option: $1"
                exit 1
                ;;
        esac
    done
    
    # Create temporary directory
    mkdir -p "${TEMP_DIR}"
    
    # Setup steps
    copy_shared_utils
    
    if [ "$SKIP_FLATBUFFERS" = false ]; then
        generate_flatbuffers || true
    fi
    
    if [ "$SKIP_CHIAVDF" = false ]; then
        build_chiavdf_mock
    fi
    
    install_python_deps
    create_test_config
    
    echo ""
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN}Test environment setup complete!${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
    echo "You can now run tests with:"
    echo "  python3 -m pytest"
    echo ""
    echo "Environment will be cleaned up automatically on exit."
    echo "To manually cleanup, run: $0 --cleanup"
    
    # If running tests immediately
    if [ "${RUN_TESTS:-false}" = true ]; then
        echo ""
        echo -e "${YELLOW}Running tests...${NC}"
        python3 -m pytest "$@"
    fi
}

# Handle cleanup-only mode
if [ "$1" = "--cleanup" ]; then
    cleanup
    exit 0
fi

# Run main setup
main "$@"