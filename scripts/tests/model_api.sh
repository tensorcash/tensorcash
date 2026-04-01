#!/usr/bin/env bash
set -euo pipefail

# Determine paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Configuration
IMAGE_TAG="core-node"
CONTAINER_NAME="core-tor"
DOCKERFILE="services/core-node/tor.Dockerfile"
HOST_PORT=8080
CONTAINER_PORT=8080

# Build the Docker image
echo "🚧 Building Docker image '${IMAGE_TAG}' from '${DOCKERFILE}'..."
sudo docker build -f "${DOCKERFILE}" -t "${IMAGE_TAG}" .

# Run the container in detached mode, setting workdir to /app
echo "🚀 Starting container '${CONTAINER_NAME}' (workdir=/app)..."
sudo docker run -d \
  --name "${CONTAINER_NAME}" \
  -w /app \
  -e TEST_MODE=true \
  -e LOG_LEVEL=DEBUG \
  -e REQUIRE_AUTH=false \
  -p ${HOST_PORT}:${CONTAINER_PORT} \
  "${IMAGE_TAG}" \
  python3 api_server.py

# Give the server a few seconds to boot
echo "⏳ Waiting for server to start on port ${HOST_PORT}..."
sleep 5

# Use curl to visualize output
echo "🌐 Hitting health endpoint:"
curl http://localhost:${HOST_PORT}/api/v1/models?extended=true

# Show the last 20 lines of logs for quick inspection
echo
echo "📝 Server logs (last 20 lines):"
sudo docker logs --tail 20 "${CONTAINER_NAME}"

# Teardown
echo
echo "🛑 Stopping and removing container '${CONTAINER_NAME}'..."
sudo docker stop "${CONTAINER_NAME}" >/dev/null
sudo docker rm "${CONTAINER_NAME}"   >/dev/null

echo "✅ Done."
