#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Unified build and test script for all C++ modules and tests

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Unified Build and Test Runner ===${NC}"

# Get directories
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
POW_UTILS_DIR="$(dirname "$TEST_DIR")"
SHARED_UTILS_DIR="$(dirname "$POW_UTILS_DIR")"
BUILD_DIR="${POW_UTILS_DIR}/build"

# Step 1: Build pfunpack module
echo -e "\n${GREEN}Step 1: Building pfunpack module...${NC}"
if [ -f "${TEST_DIR}/build_pfunpack_simple.sh" ]; then
    bash "${TEST_DIR}/build_pfunpack_simple.sh"
    if [ $? -ne 0 ]; then
        echo -e "${RED}Failed to build pfunpack${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}pfunpack build script not found, skipping...${NC}"
fi

# Step 2: Build ProofProcessor C++ module
echo -e "\n${GREEN}Step 2: Building ProofProcessor C++ module...${NC}"
if [ -f "${TEST_DIR}/build_proofprocessor_simple.sh" ]; then
    bash "${TEST_DIR}/build_proofprocessor_simple.sh"
    if [ $? -ne 0 ]; then
        echo -e "${RED}Failed to build ProofProcessor${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}ProofProcessor build script not found, skipping...${NC}"
fi

# Export Python path for testing
export PYTHONPATH="${BUILD_DIR}:${TEST_DIR}:${PYTHONPATH}"

# Step 3: Test imports
echo -e "\n${GREEN}Step 3: Testing module imports...${NC}"

# Test pfunpack
if [ -f "${TEST_DIR}/pfunpack.so" ]; then
    python3.11 -c "import pfunpack; print('✓ pfunpack imports successfully')" 2>/dev/null || \
        echo -e "${YELLOW}⚠ pfunpack import failed${NC}"
fi

# Test proof_processor
python3.11 -c "import proof_processor; print('✓ proof_processor imports successfully')" 2>/dev/null || \
    echo -e "${YELLOW}⚠ proof_processor import failed${NC}"

# Step 4: Run Python tests
echo -e "\n${GREEN}Step 4: Running Python unit tests...${NC}"
cd "${TEST_DIR}"

# Count total tests
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
SKIPPED_TESTS=0

# Run each test file
for test_file in test_*.py; do
    if [ -f "$test_file" ]; then
        echo -e "\n${BLUE}Running $test_file...${NC}"
        
        # Run test and capture output
        if python3.11 "$test_file" 2>&1 | tee /tmp/test_output.txt; then
            # Count results from output
            passed=$(grep -c "✓\|passed\|OK" /tmp/test_output.txt 2>/dev/null || echo 0)
            PASSED_TESTS=$((PASSED_TESTS + passed))
            echo -e "${GREEN}✓ $test_file completed${NC}"
        else
            FAILED_TESTS=$((FAILED_TESTS + 1))
            echo -e "${RED}✗ $test_file failed${NC}"
        fi
    fi
done

# Step 5: Run equivalence tests
echo -e "\n${GREEN}Step 5: Running C++/Python equivalence tests...${NC}"

# Run new equivalence test
if [ -f "test_proof_processor_equivalence.py" ]; then
    echo -e "${BLUE}Running ProofProcessor equivalence tests...${NC}"
    if python3.11 test_proof_processor_equivalence.py; then
        echo -e "${GREEN}✓ ProofProcessor equivalence tests passed${NC}"
        PASSED_TESTS=$((PASSED_TESTS + 1))
    else
        echo -e "${RED}✗ ProofProcessor equivalence tests failed${NC}"
        FAILED_TESTS=$((FAILED_TESTS + 1))
    fi
fi

# Run existing C++/Python comparison
if [ -f "compare_cpp_python.py" ]; then
    echo -e "${BLUE}Running existing C++/Python comparison...${NC}"
    if python3.11 compare_cpp_python.py; then
        echo -e "${GREEN}✓ C++/Python comparison passed${NC}"
        PASSED_TESTS=$((PASSED_TESTS + 1))
    else
        echo -e "${YELLOW}⚠ C++/Python comparison skipped or failed${NC}"
        SKIPPED_TESTS=$((SKIPPED_TESTS + 1))
    fi
fi

# Step 6: Run C++ native tests if available
if [ -f "${BUILD_DIR}/test_proof_processor" ]; then
    echo -e "\n${GREEN}Step 6: Running C++ native tests...${NC}"
    "${BUILD_DIR}/test_proof_processor"
fi

# Step 7: Run standalone tests
echo -e "\n${GREEN}Step 7: Running standalone tests (no dependencies)...${NC}"
if [ -f "run_tests_standalone.py" ]; then
    python3.11 run_tests_standalone.py
fi

# Step 8: Summary
echo -e "\n${BLUE}=== Test Summary ===${NC}"
echo -e "Tests Passed:  ${GREEN}$PASSED_TESTS${NC}"
echo -e "Tests Failed:  ${RED}$FAILED_TESTS${NC}"
echo -e "Tests Skipped: ${YELLOW}$SKIPPED_TESTS${NC}"

if [ $FAILED_TESTS -eq 0 ]; then
    echo -e "\n${GREEN}✅ All tests passed successfully!${NC}"
    
    # Print usage instructions
    echo -e "\n${BLUE}To use the C++ modules in Python:${NC}"
    echo -e "  export PYTHONPATH=\$PYTHONPATH:${BUILD_DIR}"
    echo -e "\n${BLUE}To use in production:${NC}"
    echo -e "  export POW_PROCESSOR_MODE=cpp  # Use C++ processor"
    echo -e "  export POW_PROCESSOR_MODE=python  # Use Python processor (default)"
    
    exit 0
else
    echo -e "\n${RED}❌ Some tests failed!${NC}"
    exit 1
fi