#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -e

echo "=== Building ProofProcessor with CMake ==="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get directories
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"

echo -e "${YELLOW}Working directory: ${SCRIPT_DIR}${NC}"

# Check if generated headers exist
if [ ! -d "${SCRIPT_DIR}/tests/build/generated-headers" ]; then
    echo -e "${YELLOW}Generated headers not found. Running build_pfunpack_simple.sh first...${NC}"
    if [ -f "${SCRIPT_DIR}/tests/build_pfunpack_simple.sh" ]; then
        cd "${SCRIPT_DIR}/tests"
        bash build_pfunpack_simple.sh
        cd "${SCRIPT_DIR}"
    else
        echo -e "${RED}build_pfunpack_simple.sh not found. Please generate FlatBuffer headers first.${NC}"
        exit 1
    fi
fi

# Create build directory
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Configure with CMake
echo -e "${GREEN}Configuring with CMake...${NC}"
cmake .. \
    -DPYTHON_EXECUTABLE=$(which python3.11 || which python3) \
    -DCMAKE_BUILD_TYPE=Release

# Build
echo -e "${GREEN}Building...${NC}"
make -j$(nproc)

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ proof_processor.so built successfully!${NC}"
    echo -e "${YELLOW}Location: ${BUILD_DIR}/proof_processor*.so${NC}"
    
    # Find the built module
    MODULE_PATH=$(find . -name "proof_processor*.so" -type f | head -n1)
    
    if [ -n "$MODULE_PATH" ]; then
        # Test import
        echo -e "${GREEN}Testing proof_processor import...${NC}"
        cd "${SCRIPT_DIR}"
        PYTHONPATH="${BUILD_DIR}:${PYTHONPATH}" python3 -c "
import proof_processor
print('✓ proof_processor imported successfully')
print('  ProofProcessor class available:', hasattr(proof_processor, 'ProofProcessor'))
"
    fi
else
    echo -e "${RED}Failed to build proof_processor.so${NC}"
    exit 1
fi

echo -e "${GREEN}=== Build complete! ===${NC}"
echo ""
echo "To use the module:"
echo "  export PYTHONPATH=\"${BUILD_DIR}:\$PYTHONPATH\""
echo "  export POW_PROCESSOR_MODE=cpp"
echo ""
echo "To run tests:"
echo "  cd ${SCRIPT_DIR}/tests"
echo "  PYTHONPATH=\"${BUILD_DIR}:\$PYTHONPATH\" python3 test_proof_processor_equivalence.py"