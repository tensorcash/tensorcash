#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Validation script to check if the C++ build will work in CI

set -e

echo "=== Validating C++ Build Setup ==="

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

errors=0

# Check for system dependencies
echo -e "${YELLOW}Checking system dependencies...${NC}"

deps=(
    "g++"
    "make"
    "pkg-config"
    "wget"
    "unzip"
)

for dep in "${deps[@]}"; do
    if command -v "$dep" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ $dep available${NC}"
    else
        echo -e "${RED}✗ $dep missing${NC}"
        errors=$((errors + 1))
    fi
done

# Check for libraries (note: headers may not be available without dev packages)
echo -e "${YELLOW}Checking for library availability...${NC}"

# Check if we can find OpenSSL
if pkg-config --exists openssl || [ -f "/usr/lib/x86_64-linux-gnu/libssl.so" ] || [ -f "/usr/lib/libssl.so" ]; then
    echo -e "${GREEN}✓ OpenSSL library available${NC}"
else
    echo -e "${YELLOW}⚠ OpenSSL library may need installation${NC}"
fi

# Check if we can find ZMQ
if pkg-config --exists libzmq || [ -f "/usr/lib/x86_64-linux-gnu/libzmq.so" ] || [ -f "/usr/lib/libzmq.so" ]; then
    echo -e "${GREEN}✓ ZMQ library available${NC}"
else
    echo -e "${YELLOW}⚠ ZMQ library may need installation${NC}"
fi

# Check source files
echo -e "${YELLOW}Checking source files...${NC}"

sources=(
    "../pow_utils.cpp"
    "../pow_test.cpp"
    "../pow_utils.h"
    "../Makefile"
)

for src in "${sources[@]}"; do
    if [ -f "$src" ]; then
        echo -e "${GREEN}✓ $(basename $src) exists${NC}"
    else
        echo -e "${RED}✗ $(basename $src) missing${NC}"
        errors=$((errors + 1))
    fi
done

# Check chiavdf
echo -e "${YELLOW}Checking chiavdf...${NC}"
if [ -d "../../chiavdf" ]; then
    echo -e "${GREEN}✓ chiavdf directory exists${NC}"
    
    # Check for chiavdf tests
    if [ -f "../../chiavdf/tests/test_verifier.py" ]; then
        echo -e "${GREEN}✓ chiavdf tests exist${NC}"
    else
        echo -e "${YELLOW}⚠ chiavdf tests may be missing${NC}"
    fi
else
    echo -e "${RED}✗ chiavdf directory missing${NC}"
    errors=$((errors + 1))
fi

# Test internet connectivity for downloads
echo -e "${YELLOW}Testing internet connectivity...${NC}"
if wget -q --spider https://github.com/google/flatbuffers/archive/v23.5.26.tar.gz; then
    echo -e "${GREEN}✓ Can download FlatBuffers${NC}"
else
    echo -e "${RED}✗ Cannot download FlatBuffers${NC}"
    errors=$((errors + 1))
fi

if wget -q --spider https://ftp.gnu.org/gnu/gmp/gmp-6.3.0.tar.xz; then
    echo -e "${GREEN}✓ Can download GMP${NC}"
else
    echo -e "${RED}✗ Cannot download GMP${NC}"
    errors=$((errors + 1))
fi

# Summary
echo ""
echo -e "${YELLOW}=== Validation Summary ===${NC}"
if [ $errors -eq 0 ]; then
    echo -e "${GREEN}✓ All basic checks passed!${NC}"
    echo ""
    echo "The CI will attempt to:"
    echo "1. Install libssl-dev, libzmq3-dev, libgmp-dev, libflint-dev"
    echo "2. Download and build FlatBuffers v23.5.26 from source"
    echo "3. Build C++ tests: pow_test, compile_test, debug_messages"
    echo "4. Download and build GMP 6.3.0 with ASM support"
    echo "5. Build and test chiavdf wheel"
    echo "6. Run comprehensive test suite"
    echo ""
    echo -e "${YELLOW}Note: Some dependencies will be installed by CI script${NC}"
    exit 0
else
    echo -e "${RED}✗ Found $errors issues that may cause CI to fail${NC}"
    echo ""
    echo "Most issues can be resolved by CI dependency installation,"
    echo "but missing source files need to be fixed locally."
    exit 1
fi