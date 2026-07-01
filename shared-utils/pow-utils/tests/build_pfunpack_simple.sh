#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -e

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

echo "=== Building pfunpack with simple compilation ==="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the test directory path
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${TEST_DIR}/build"
FB_SCHEMAS_DIR="$REPO_ROOT/shared-utils/fb-schemas"
PFUNPACK_DIR="$REPO_ROOT/shared-utils/pow-utils/pfunpack"

echo -e "${YELLOW}Working directory: ${TEST_DIR}${NC}"

# Step 1: Check if we can use existing flatc
echo -e "${GREEN}Looking for flatc...${NC}"
FLATC=""

# Check common locations
if [ -f "/usr/local/bin/flatc" ]; then
    FLATC="/usr/local/bin/flatc"
elif [ -f "/usr/bin/flatc" ]; then
    FLATC="/usr/bin/flatc"
elif [ -f "${HOME}/.local/bin/flatc" ]; then
    FLATC="${HOME}/.local/bin/flatc"
else
    echo -e "${YELLOW}flatc not found in standard locations${NC}"
    # Try to download pre-built binary
    echo -e "${GREEN}Downloading pre-built flatc binary...${NC}"
    mkdir -p "${BUILD_DIR}"
    cd "${BUILD_DIR}"
    
    if [ ! -f "flatc" ]; then
        wget -q "https://github.com/google/flatbuffers/releases/download/v23.5.26/Linux.flatc.binary.g%2B%2B-10.zip"
        unzip -q "Linux.flatc.binary.g++-10.zip"
        chmod +x flatc
        rm -f "Linux.flatc.binary.g++-10.zip"
    fi
    FLATC="${BUILD_DIR}/flatc"
fi

echo -e "${GREEN}Using flatc: ${FLATC}${NC}"
"${FLATC}" --version || echo "No version info available"

# Step 2: Generate FlatBuffers headers in build directory
echo -e "${GREEN}Generating FlatBuffers headers in build directory...${NC}"
GENERATED_HEADERS_DIR="${BUILD_DIR}/generated-headers"
mkdir -p "${GENERATED_HEADERS_DIR}"

cd "${FB_SCHEMAS_DIR}"
"${FLATC}" --cpp -o "${GENERATED_HEADERS_DIR}" proof.fbs
"${FLATC}" --cpp -o "${GENERATED_HEADERS_DIR}" validation.fbs  
"${FLATC}" --cpp -o "${GENERATED_HEADERS_DIR}" blockheader.fbs

echo -e "${GREEN}Headers generated in ${GENERATED_HEADERS_DIR}${NC}"

# Step 3: Try to compile pfunpack.cpp directly
echo -e "${GREEN}Compiling pfunpack.so...${NC}"

# Get Python include path
PYTHON_INCLUDE=$(python3.11 -c "from sysconfig import get_paths; print(get_paths()['include'])")
PYBIND_INCLUDE=$(python3.11 -c "import pybind11; print(pybind11.get_include())")

# Check for FlatBuffers headers
FLATBUFFERS_INCLUDE=""
if [ -d "/usr/local/include/flatbuffers" ]; then
    FLATBUFFERS_INCLUDE="/usr/local/include"
elif [ -d "/usr/include/flatbuffers" ]; then
    FLATBUFFERS_INCLUDE="/usr/include"
else
    # Download FlatBuffers headers if not found
    echo -e "${YELLOW}FlatBuffers headers not found, downloading...${NC}"
    mkdir -p "${BUILD_DIR}/flatbuffers-headers"
    cd "${BUILD_DIR}"
    
    if [ ! -d "flatbuffers-headers/flatbuffers" ]; then
        wget -q https://github.com/google/flatbuffers/archive/v23.5.26.tar.gz
        tar -xzf v23.5.26.tar.gz
        cp -r flatbuffers-23.5.26/include/flatbuffers flatbuffers-headers/
        rm -rf flatbuffers-23.5.26 v23.5.26.tar.gz
    fi
    FLATBUFFERS_INCLUDE="${BUILD_DIR}/flatbuffers-headers"
fi

echo -e "${GREEN}Using FlatBuffers headers from: ${FLATBUFFERS_INCLUDE}${NC}"

# Copy pfunpack.cpp to build directory and modify includes
cp "${PFUNPACK_DIR}/pfunpack.cpp" "${BUILD_DIR}/pfunpack_build.cpp"

# Update includes to use generated headers from build directory
sed -i 's|#include "proof_generated.h"|#include "proof_generated.h"|' "${BUILD_DIR}/pfunpack_build.cpp"
sed -i 's|#include "validation_generated.h"|#include "validation_generated.h"|' "${BUILD_DIR}/pfunpack_build.cpp"
sed -i 's|#include "blockheader_generated.h"|#include "blockheader_generated.h"|' "${BUILD_DIR}/pfunpack_build.cpp"

# Compile pfunpack
cd "${TEST_DIR}"
g++ -O3 -Wall -shared -std=c++17 -fPIC \
    -I"${PYTHON_INCLUDE}" \
    -I"${PYBIND_INCLUDE}" \
    -I"${FLATBUFFERS_INCLUDE}" \
    -I"${GENERATED_HEADERS_DIR}" \
    "${BUILD_DIR}/pfunpack_build.cpp" \
    -o pfunpack.so \
    $(python3.11 -m pybind11 --includes) \
    $(python3.11-config --ldflags)

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ pfunpack.so built successfully!${NC}"
    echo -e "${YELLOW}Location: ${TEST_DIR}/pfunpack.so${NC}"
    
    # Test import
    echo -e "${GREEN}Testing pfunpack import...${NC}"
    python3.11 -c "import sys; sys.path.insert(0, '${TEST_DIR}'); import pfunpack; print('✓ pfunpack imported successfully')"
else
    echo -e "${RED}Failed to build pfunpack.so${NC}"
    exit 1
fi

echo -e "${GREEN}=== Build complete! ===${NC}"
