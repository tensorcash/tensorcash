#!/usr/bin/env bash
set -euo pipefail

# Determine this script's directory...
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ...then assume project root is two levels up (from scripts/tests → root)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Switch to project root
cd "$PROJECT_ROOT"

# Configuration
IMAGE_TAG="core-node"
CONTAINER_NAME="core-tor"
DOCKERFILE="services/core-node/tor.Dockerfile"

# Build the Docker image
echo "🚧 Building Docker image '${IMAGE_TAG}' from '${DOCKERFILE}'..."
sudo docker build -f "${DOCKERFILE}" -t "${IMAGE_TAG}" .