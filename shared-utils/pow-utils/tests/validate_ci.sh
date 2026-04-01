#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Validation script to check CI readiness

set -e

echo "=== Validating CI Setup for pow-utils tests ==="

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

errors=0

# Check Python version
echo -e "${YELLOW}Checking Python 3.11...${NC}"
if python3.11 --version > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Python 3.11 available${NC}"
else
    echo -e "${RED}✗ Python 3.11 not found${NC}"
    errors=$((errors + 1))
fi

# Check test files exist
echo -e "${YELLOW}Checking test files...${NC}"
test_files=(
    "test_byte_conversions.py"
    "test_sha256_message.py"
    "test_difficulty_arithmetic.py"
    "test_row_manager.py"
    "test_ring_buffers.py"
    "test_proof_writer.py"
    "test_pow_hasher.py"
    "test_uint256_arithmetics.py"
    "test_pfunpack_roundtrip.py"
    "test_pfunpack_comprehensive.py"
    "test_cpp_minimal.cpp"
)

for file in "${test_files[@]}"; do
    if [ -f "$file" ]; then
        echo -e "${GREEN}✓ $file exists${NC}"
    else
        echo -e "${RED}✗ $file missing${NC}"
        errors=$((errors + 1))
    fi
done

# Check parent directory files
echo -e "${YELLOW}Checking pow-utils modules...${NC}"
if [ -f "../pow_utils.py" ]; then
    echo -e "${GREEN}✓ pow_utils.py exists${NC}"
else
    echo -e "${RED}✗ pow_utils.py missing${NC}"
    errors=$((errors + 1))
fi

if [ -f "../uint256_arithmetics.py" ]; then
    echo -e "${GREEN}✓ uint256_arithmetics.py exists${NC}"
else
    echo -e "${RED}✗ uint256_arithmetics.py missing${NC}"
    errors=$((errors + 1))
fi

# Check FlatBuffers schemas
echo -e "${YELLOW}Checking FlatBuffers schemas...${NC}"
fb_schemas=(
    "../../fb-schemas/proof.fbs"
    "../../fb-schemas/validation.fbs"
    "../../fb-schemas/blockheader.fbs"
)

for schema in "${fb_schemas[@]}"; do
    if [ -f "$schema" ]; then
        echo -e "${GREEN}✓ $(basename $schema) exists${NC}"
    else
        echo -e "${RED}✗ $(basename $schema) missing${NC}"
        errors=$((errors + 1))
    fi
done

# Check pfunpack source
echo -e "${YELLOW}Checking pfunpack source...${NC}"
if [ -f "../../pow-utils/pfunpack/pfunpack.cpp" ]; then
    echo -e "${GREEN}✓ pfunpack.cpp exists in shared-utils${NC}"
else
    echo -e "${RED}✗ pfunpack.cpp missing from shared-utils/pow-utils/pfunpack${NC}"
    errors=$((errors + 1))
fi

# Check .gitignore files
echo -e "${YELLOW}Checking .gitignore files...${NC}"
if [ -f ".gitignore" ]; then
    echo -e "${GREEN}✓ tests/.gitignore exists${NC}"
else
    echo -e "${YELLOW}⚠ tests/.gitignore missing (optional)${NC}"
fi

if [ -f "../../fb-schemas/.gitignore" ]; then
    echo -e "${GREEN}✓ fb-schemas/.gitignore exists${NC}"
else
    echo -e "${YELLOW}⚠ fb-schemas/.gitignore missing (recommended)${NC}"
fi

# Check requirements file
echo -e "${YELLOW}Checking requirements...${NC}"
if [ -f "requirements.txt" ]; then
    echo -e "${GREEN}✓ requirements.txt exists${NC}"
else
    echo -e "${YELLOW}⚠ requirements.txt missing (affects caching)${NC}"
fi

# Summary
echo ""
echo -e "${YELLOW}=== Validation Summary ===${NC}"
if [ $errors -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed! CI should run successfully.${NC}"
    echo ""
    echo "The GitHub Actions workflow will:"
    echo "1. Run on Python 3.11"
    echo "2. Build FlatBuffers v23.5.26"
    echo "3. Compile pfunpack.so"
    echo "4. Run 90+ Python tests"
    echo "5. Run 5 C++ tests"
    echo "6. Report coverage to Codecov"
    exit 0
else
    echo -e "${RED}✗ Found $errors issues that need to be fixed${NC}"
    exit 1
fi
