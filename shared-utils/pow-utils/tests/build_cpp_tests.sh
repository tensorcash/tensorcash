#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -e

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

echo "=== Building C++ Tests (without ZMQ dependency) ==="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the test directory path
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
POW_UTILS_DIR="$(dirname "$TEST_DIR")"
BUILD_DIR="${TEST_DIR}/build"

echo -e "${YELLOW}Working directory: ${TEST_DIR}${NC}"
echo -e "${YELLOW}Pow-utils directory: ${POW_UTILS_DIR}${NC}"

# Create a modified pow_utils.h without ZMQ dependency
echo -e "${GREEN}Creating test version of pow_utils.h without ZMQ...${NC}"
cp "${POW_UTILS_DIR}/pow_utils.h" "${BUILD_DIR}/pow_utils_test.h"

# Comment out the ZMQ include
sed -i 's|#include "pow_zmq_writer.h"|// #include "pow_zmq_writer.h" // Disabled for testing|' "${BUILD_DIR}/pow_utils_test.h"

# Create a modified pow_utils.cpp for testing
echo -e "${GREEN}Creating test version of pow_utils.cpp...${NC}"
cp "${POW_UTILS_DIR}/pow_utils.cpp" "${BUILD_DIR}/pow_utils_test.cpp"

# Replace the header include
sed -i 's|#include "pow_utils.h"|#include "pow_utils_test.h"|' "${BUILD_DIR}/pow_utils_test.cpp"

# Comment out ZMQ-related code
sed -i 's|MiningResponseSubmitter::submit|// MiningResponseSubmitter::submit|' "${BUILD_DIR}/pow_utils_test.cpp"

# Copy test file
cp "${POW_UTILS_DIR}/pow_test.cpp" "${BUILD_DIR}/pow_test.cpp"
sed -i 's|#include "pow_utils.h"|#include "pow_utils_test.h"|' "${BUILD_DIR}/pow_test.cpp"

# Compile C++ tests
echo -e "${GREEN}Compiling C++ tests...${NC}"
cd "${BUILD_DIR}"

g++ -std=c++17 -Wall -Wextra -O2 -g -Wno-deprecated-declarations \
    -I"${BUILD_DIR}" \
    -I"${BUILD_DIR}/flatbuffers-headers" \
    -I"$REPO_ROOT/shared-utils/fb-schemas" \
    -c pow_utils_test.cpp -o pow_utils_test.o

g++ -std=c++17 -Wall -Wextra -O2 -g -Wno-deprecated-declarations \
    -I"${BUILD_DIR}" \
    -I"${BUILD_DIR}/flatbuffers-headers" \
    -I"$REPO_ROOT/shared-utils/fb-schemas" \
    -c pow_test.cpp -o pow_test.o

g++ pow_utils_test.o pow_test.o -o pow_test -lssl -lcrypto

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ C++ tests built successfully!${NC}"
    echo -e "${YELLOW}Binary location: ${BUILD_DIR}/pow_test${NC}"
    
    # Run the tests
    echo -e "${GREEN}Running C++ tests...${NC}"
    ./pow_test
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ All C++ tests passed!${NC}"
    else
        echo -e "${RED}Some C++ tests failed${NC}"
        exit 1
    fi
else
    echo -e "${RED}Failed to build C++ tests${NC}"
    exit 1
fi

echo -e "${GREEN}=== C++ test build and run complete! ===${NC}"