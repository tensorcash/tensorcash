#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Generate proving and verification keys

set -e

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════════════"
echo "  TensorCash KYC Prover - Key Generation"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Check if Go is installed
if ! command -v go &> /dev/null; then
    echo "✗ Go is not installed"
    echo ""
    echo "Install Go 1.21+ from: https://go.dev/dl/"
    exit 1
fi

echo "✓ Go version: $(go version | cut -d' ' -f3)"
echo ""

# Download dependencies
echo "► Downloading dependencies..."
go mod download
echo "✓ Dependencies downloaded"
echo ""

# Build server
echo "► Building server..."
go build -o kyc-prover ./cmd/server
echo "✓ Server built: ./kyc-prover"
echo ""

# Run setup to generate keys
echo "► Generating cryptographic keys..."
echo ""
./kyc-prover -setup -pk proving_key.bin -vk verification_key.bin

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Setup Complete!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Files created:"
echo "  • proving_key.bin (~140 MB)"
echo "  • verification_key.bin (~2 KB)"
echo ""
echo "Next steps:"
echo "  1. Start service: ./kyc-prover -port 8080"
echo "  2. Test: curl http://localhost:8080/health"
echo "  3. Or use Docker: docker-compose up -d"
echo ""
