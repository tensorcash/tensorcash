#!/usr/bin/env bash
set -euo pipefail

# Resolve script location and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Determine processed-data directory (default to /data)
if [ "$#" -ge 1 ] && [ -d "$1" ]; then
  PROCESSED_DATA_DIR="$1"
else
  PROCESSED_DATA_DIR="/data"
fi

# Hard-coded test command
CONTAINER_CMD=( "python" "src/tests/test_streaming_verifier.py" )

# Docker settings
IMAGE_TAG="verification-api"
CONTAINER_NAME="verification-api"
DOCKERFILE="services/verification-api/generic.Dockerfile"

# Build with BuildKit
echo "🔨 Building Docker image ${IMAGE_TAG}..."
sudo DOCKER_BUILDKIT=1 docker build -f "${DOCKERFILE}" -t "${IMAGE_TAG}" .

# Run with GPU support, mounting your data, and executing the hard-coded test
echo "🚀 Running container ${CONTAINER_NAME}, ${IMAGE_TAG}, mounting '${PROCESSED_DATA_DIR}', executing: ${CONTAINER_CMD[*]}"
sudo docker run --gpus all -it --rm \
  --name "${CONTAINER_NAME}" \
  # -v "${PROCESSED_DATA_DIR}:/data:rw" \
  "${IMAGE_TAG}" "${CONTAINER_CMD[@]}"

# example usage "cd /scripts/tests && ./chia_vdf.sh"