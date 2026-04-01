#!/bin/bash
# Copyright (c) 2025 The TensorCash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://opensource.org/license/mit/.

# Docker wrapper for E2E Bulletin Board Tests
# Runs the complete test suite inside a Docker container

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_info "Starting E2E Bulletin Board Tests in Docker"
log_info "Project root: $PROJECT_ROOT"

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed or not in PATH"
    exit 1
fi

# Build the Rust project and run tests in Docker
log_info "Building cosign-bridge in Docker..."

docker run --rm \
    -v "${PROJECT_ROOT}:/workspace" \
    -w /workspace/services/core-node/cosign-bridge \
    --network host \
    rust:1.83 \
    bash -c "
        set -e
        echo '=== Installing dependencies ==='
        apt-get update -qq && apt-get install -y -qq jq > /dev/null 2>&1

        echo '=== Building cosign-bridge ==='
        cargo build 2>&1 | grep -E '(Compiling|Finished|error|warning:)' || true

        if [ ! -f target/debug/cosign-bridge ]; then
            echo 'ERROR: Build failed - binary not found'
            exit 1
        fi

        echo '=== Running E2E tests ==='
        chmod +x tests/e2e_bulletin_board_simple.sh
        ./tests/e2e_bulletin_board_simple.sh
    "

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log_info "✓ E2E tests completed successfully"
else
    log_error "✗ E2E tests failed with exit code $EXIT_CODE"
fi

exit $EXIT_CODE
