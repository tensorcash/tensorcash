#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Single comprehensive test runner for PoW utils - Python 3.11

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== PoW Utils Test Runner ===${NC}"
echo ""

# Get directories
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
POW_UTILS_DIR="$(dirname "$SCRIPT_DIR")"

# Force Python 3.11
PYTHON_CMD="python3.11"

# Check Python 3.11 is available
if ! command -v python3.11 &> /dev/null; then
    echo -e "${RED}Error: Python 3.11 is required but not found${NC}"
    echo "Please install Python 3.11 or update the PYTHON_CMD variable"
    exit 1
fi

echo -e "${YELLOW}Using Python:${NC} $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"
echo -e "${YELLOW}Test directory:${NC} $SCRIPT_DIR"
echo ""

# Generate FlatBuffers if needed
PROOF_MODULE="$POW_UTILS_DIR/proof"
FB_SCHEMAS_DIR="$POW_UTILS_DIR/../fb-schemas"

# Check if flatc is available
if command -v flatc &> /dev/null; then
    echo -e "${YELLOW}Generating FlatBuffers Python modules...${NC}"
    cd "$POW_UTILS_DIR"
    flatc --python "$FB_SCHEMAS_DIR/proof.fbs" 2>/dev/null || true
    flatc --python "$FB_SCHEMAS_DIR/validation.fbs" 2>/dev/null || true
    flatc --python "$FB_SCHEMAS_DIR/blockheader.fbs" 2>/dev/null || true
    cd "$SCRIPT_DIR"
fi

# Create mock proof module if FlatBuffers generation failed or flatc not available
if [ ! -f "$PROOF_MODULE/Proof.py" ]; then
    if [ ! -d "$PROOF_MODULE" ]; then
        mkdir -p "$PROOF_MODULE"
    fi
    cat > "$PROOF_MODULE/__init__.py" << 'EOF'
# Mock proof module for testing
class FloatArray:
    def __init__(self):
        self.values = []

class UIntArray:
    def __init__(self):
        self.values = []

class Proof:
    def __init__(self):
        pass
EOF
fi

# Set PYTHONPATH
export PYTHONPATH="$POW_UTILS_DIR:$PYTHONPATH"

# Install dependencies for Python 3.11
echo -e "${YELLOW}Installing dependencies for Python 3.11...${NC}"

# Install torch (CPU version)
$PYTHON_CMD -c "import torch" 2>/dev/null || {
    echo "  Installing torch (CPU version)..."
    $PYTHON_CMD -m pip install --user torch --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || {
        echo -e "${YELLOW}  Warning: Could not install torch automatically${NC}"
    }
}

# Install other dependencies
$PYTHON_CMD -c "import pytest" 2>/dev/null || {
    echo "  Installing pytest..."
    $PYTHON_CMD -m pip install --user pytest pytest-cov 2>/dev/null || true
}

$PYTHON_CMD -c "import numpy" 2>/dev/null || {
    echo "  Installing numpy..."
    $PYTHON_CMD -m pip install --user numpy 2>/dev/null || true
}

$PYTHON_CMD -c "import flatbuffers" 2>/dev/null || {
    echo "  Installing flatbuffers..."
    $PYTHON_CMD -m pip install --user flatbuffers 2>/dev/null || true
}

# Check if imports work
echo -e "${YELLOW}Testing imports...${NC}"
$PYTHON_CMD -c "
import sys
sys.path.insert(0, '$POW_UTILS_DIR')
try:
    import torch
    print('  ✓ torch available')
except:
    print('  ⚠ torch not available (tests will fail)')
try:
    from pow_utils import hex_to_bytes_tensor, RowManager, RingBuffers
    print('  ✓ pow_utils imports working')
except Exception as e:
    print(f'  ✗ pow_utils import failed: {e}')
"

# Run tests with pytest if available
echo ""
if $PYTHON_CMD -c "import pytest" 2>/dev/null; then
    echo -e "${GREEN}Running tests with pytest...${NC}"
    cd "$SCRIPT_DIR"
    
    $PYTHON_CMD -m pytest \
        test_byte_conversions.py \
        test_sha256_message.py \
        test_difficulty_arithmetic.py \
        test_row_manager.py \
        test_ring_buffers.py \
        test_proof_writer.py \
        -v \
        --tb=short \
        --color=yes \
        -W ignore::DeprecationWarning \
        2>&1 | tee test_output.log
    
    RESULT=${PIPESTATUS[0]}
else
    echo -e "${YELLOW}pytest not available, running simple verification...${NC}"
    cd "$SCRIPT_DIR"
    $PYTHON_CMD test_simple_verify.py
    RESULT=$?
fi

if [ $RESULT -eq 0 ]; then
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✅ ALL TESTS PASSED!${NC}"
    echo -e "${GREEN}========================================${NC}"
else
    echo ""
    echo -e "${RED}Some tests failed. Check test_output.log for details.${NC}"
fi

# Always attempt the cross-language comparison as a final check
echo ""
echo -e "${YELLOW}Running C++/Python cross-language comparison...${NC}"
cd "$SCRIPT_DIR"
$PYTHON_CMD compare_cpp_python.py || true

exit $RESULT
