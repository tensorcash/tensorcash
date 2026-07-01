#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Generate golden test vectors

set -e

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════════════"
echo "  Generating Golden Test Vectors"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Check if Go is installed
if ! command -v go &> /dev/null; then
    echo "✗ Go not found"
    exit 1
fi

# Build gentest tool
echo "► Building vector generator..."
go build -o gentest ./cmd/gentest
echo "✓ Generator built"
echo ""

# Generate vectors
echo "► Generating vectors..."
./gentest -output vectors

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Done!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Vectors saved to: vectors/golden_vectors.json"
echo ""
echo "Use these vectors in tests to ensure deterministic behavior."
echo ""
