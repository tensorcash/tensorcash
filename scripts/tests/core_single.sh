#!/usr/bin/env bash
set -euo pipefail

# Determine this script's directory...
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ...then assume project root is two levels up (from scripts/tests → root)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Switch to project root
cd "$PROJECT_ROOT"

# Usage check
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <bitcoin-data-dir> <tor-data-dir>"
  exit 1
fi

# Arguments
DATA_DIR="$1"
TOR_DIR="$2"

# Configuration
IMAGE_TAG="core-node"
CONTAINER_NAME="core-tor"
DOCKERFILE="services/core-node/tor.Dockerfile"

# Build the Docker image
echo "🚧 Building Docker image '${IMAGE_TAG}' from '${DOCKERFILE}'..."
sudo docker build -f "${DOCKERFILE}" -t "${IMAGE_TAG}" .

echo "🏃 Running container '${CONTAINER_NAME}'"
sudo docker run -it --rm \
  --name "${CONTAINER_NAME}" \
  -e RPC_USER=foo \
  -e RPC_PASS=bar \
  -e MODEL_API_KEY=baz \
  -v "${DATA_DIR}:/data" \
  -v "${TOR_DIR}:/var/lib/tor" \
  -p 8333:8333 \
  -p 8332:8332 \
  -p 8050:8050 \
  "${IMAGE_TAG}"