#!/bin/bash
# Local test script for cosign-bridge
# Run this before pushing to ensure CI will pass

set -e

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BRIDGE_DIR"

echo "========================================"
echo "Cosign-Bridge Local Test Suite"
echo "========================================"
echo ""

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to run commands with status
run_step() {
    local desc="$1"
    shift
    echo -e "${YELLOW}▶${NC} $desc..."
    if "$@"; then
        echo -e "${GREEN}✓${NC} $desc passed"
        echo ""
        return 0
    else
        echo -e "${RED}✗${NC} $desc failed"
        exit 1
    fi
}

# Step 1: Check formatting
run_step "Checking Rust formatting" cargo fmt --all -- --check

# Step 2: Run clippy (linter)
run_step "Running clippy (linter)" cargo clippy --all-targets --all-features -- -D warnings

# Step 3: Build release
run_step "Building release binary" cargo build --release

# Step 4: Run all tests
run_step "Running unit tests" cargo test --verbose -- --test-threads=1

# Step 5: Generate coverage (optional, requires cargo-tarpaulin)
if command -v cargo-tarpaulin &> /dev/null; then
    echo -e "${YELLOW}▶${NC} Generating coverage report..."
    cargo tarpaulin \
        --out Html \
        --output-dir target/coverage \
        --exclude-files 'target/*' \
        -- --test-threads=1
    echo -e "${GREEN}✓${NC} Coverage report: target/coverage/index.html"
    echo ""
else
    echo -e "${YELLOW}⚠${NC}  cargo-tarpaulin not installed, skipping coverage"
    echo "   Install with: cargo install cargo-tarpaulin"
    echo ""
fi

# Step 6: Check binary size
echo -e "${YELLOW}▶${NC} Binary size check..."
ls -lh target/release/cosign-bridge
SIZE=$(stat -c%s target/release/cosign-bridge 2>/dev/null || stat -f%z target/release/cosign-bridge)
SIZE_MB=$((SIZE / 1024 / 1024))
echo "Binary size: ${SIZE_MB} MB"
echo ""

# Summary
echo "========================================"
echo -e "${GREEN}✓ All local tests passed!${NC}"
echo "========================================"
echo ""
echo "Ready to push to CI"
echo ""
echo "Test coverage details:"
echo "  - Crypto module: SPAKE2 + Noise Protocol"
echo "  - Protocol module: Message framing + padding"
echo "  - Session module: Lifecycle + rate limiting"
echo "  - Transport module: WebSocket client"
echo "  - Stdio module: HWI-style JSON protocol"
