#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -e

echo "=== Building pfunpack with FlatBuffers v23.5.26 ==="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the test directory path
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${TEST_DIR}/build"
FLATBUFFERS_DIR="${BUILD_DIR}/flatbuffers"
FLATBUFFERS_INSTALL="${BUILD_DIR}/flatbuffers-install"

echo -e "${YELLOW}Working directory: ${TEST_DIR}${NC}"
echo -e "${YELLOW}Build directory: ${BUILD_DIR}${NC}"

# Create build directory
mkdir -p "${BUILD_DIR}"

# Step 1: Build FlatBuffers if not already done
if [ ! -f "${FLATBUFFERS_INSTALL}/bin/flatc" ]; then
    echo -e "${GREEN}Building FlatBuffers v23.5.26...${NC}"
    
    # Clone if not exists
    if [ ! -d "${FLATBUFFERS_DIR}" ]; then
        cd "${BUILD_DIR}"
        git clone --depth 1 --branch v23.5.26 https://github.com/google/flatbuffers.git
    fi
    
    # Build FlatBuffers
    cd "${FLATBUFFERS_DIR}"
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${FLATBUFFERS_INSTALL}" \
        -DFLATBUFFERS_BUILD_TESTS=OFF \
        -DFLATBUFFERS_BUILD_FLATLIB=ON \
        -DFLATBUFFERS_BUILD_FLATC=ON
    
    cmake --build build -j$(nproc)
    cmake --install build
    
    echo -e "${GREEN}FlatBuffers installed to ${FLATBUFFERS_INSTALL}${NC}"
else
    echo -e "${YELLOW}FlatBuffers already built, skipping...${NC}"
fi

# Step 2: Generate FlatBuffers headers
echo -e "${GREEN}Generating FlatBuffers headers...${NC}"
FLATC="${FLATBUFFERS_INSTALL}/bin/flatc"
FB_SCHEMAS_DIR="${FB_SCHEMAS_DIR:-$(cd "${TEST_DIR}/../../fb-schemas" && pwd)}"

cd "${FB_SCHEMAS_DIR}"
"${FLATC}" --cpp proof.fbs
"${FLATC}" --cpp validation.fbs  
"${FLATC}" --cpp blockheader.fbs

# Copy generated headers to pfunpack source directory in shared-utils
PFUNPACK_DIR="${TEST_DIR}/../pfunpack"
cp -f *_generated.h "${PFUNPACK_DIR}/"

# Step 3: Build pfunpack.so
echo -e "${GREEN}Building pfunpack.so...${NC}"
PFUNPACK_BUILD="${BUILD_DIR}/pfunpack-build"
mkdir -p "${PFUNPACK_BUILD}"

cd "${PFUNPACK_BUILD}"
cmake "${PFUNPACK_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DFLATBUFFERS_INSTALL_DIR="${FLATBUFFERS_INSTALL}" \
    -Dpybind11_DIR="$(python3 -c 'import pybind11; print(pybind11.get_cmake_dir())')"

cmake --build . -j$(nproc)

# Copy pfunpack.so to test directory
cp -f pfunpack.so "${TEST_DIR}/"

echo -e "${GREEN}✓ pfunpack.so built successfully!${NC}"
echo -e "${YELLOW}Location: ${TEST_DIR}/pfunpack.so${NC}"

# Step 4: Test import
echo -e "${GREEN}Testing pfunpack import...${NC}"
cd "${TEST_DIR}"
python3 -c "import pfunpack; print('✓ pfunpack imported successfully')"

echo -e "${GREEN}=== Build complete! ===${NC}"
