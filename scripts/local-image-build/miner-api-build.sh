#!/usr/bin/env bash
set -euo pipefail

# Resolve script location and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Defaults
DEFAULT_CU="cu123"
CU_VERSION=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [-c CU_VERSION]
  -c  CUDA version prefix: cu120 | cu123 | cu126 (default: auto-detect or cu123)
EOF
  exit 1
}

# Parse options
while getopts "c:h" opt; do
  case "$opt" in
    c) CU_VERSION="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done
shift $((OPTIND-1))

# Function to map CU version to CUDA version
map_cu_to_cuda() {
  local cu_ver="$1"
  case "$cu_ver" in
    cu120) echo "12.0.0" ;;
    cu123) echo "12.3.0" ;;
    cu126) echo "12.6.0" ;;
    *) echo "12.3.0" ;;  # default
  esac
}

# Function to map CU version to vLLM version
map_cu_to_vllm() {
  local cu_ver="$1"
  case "$cu_ver" in
    cu120) echo "0.10.0" ;;
    cu123) echo "0.10.0" ;;
    cu126) echo "0.10.0" ;;
    *) echo "0.10.0" ;;  # default
  esac
}

# Determine CU_VERSION
if [[ -n "$CU_VERSION" ]]; then
  # User override: validate
  if [[ ! "$CU_VERSION" =~ ^cu(120|123|126)$ ]]; then
    echo "⚠️  Invalid CUDA prefix '$CU_VERSION'; must be cu120, cu123, or cu126."
    exit 1
  fi
else
  # Auto-detect via nvidia-smi
  if command -v nvidia-smi >/dev/null 2>&1; then
    if RAW="$(nvidia-smi --query-gpu=cuda_version --format=csv,noheader,nounits 2>/dev/null)"; then
      VER="$(echo "$RAW" | head -n1)"
    else
      VER="$(nvidia-smi | grep -i 'CUDA Version:' \
             | sed -E 's/.*CUDA Version: *([0-9]+\.[0-9]+).*/\1/' \
             | head -n1 || true)"
    fi
    
    if [[ -n "$VER" ]]; then
      SHORT="${VER/./}"
      CU="cu${SHORT}"
      if [[ "$CU" =~ ^cu(120|123|126)$ ]]; then
        CU_VERSION="$CU"
      else
        CU_VERSION="$DEFAULT_CU"
      fi
    else
      CU_VERSION="$DEFAULT_CU"
    fi
  else
    CU_VERSION="$DEFAULT_CU"
  fi
fi

# Map to versions
CUDA_VERSION=$(map_cu_to_cuda "$CU_VERSION")
VLLM_VERSION=$(map_cu_to_vllm "$CU_VERSION")

echo "🔧 Using CUDA build: ${CU_VERSION} (CUDA ${CUDA_VERSION}, vLLM ${VLLM_VERSION})"

# Build backend image
IMAGE_TAG="vllm-miner-api-backend"
DOCKERFILE="services/miner-api/vllm_generic.Dockerfile"

echo "🔨 Building Docker image ${IMAGE_TAG} with ${DOCKERFILE}..."
sudo DOCKER_BUILDKIT=1 docker build \
  -f "${DOCKERFILE}" \
  --build-arg CUDA_VERSION="${CUDA_VERSION}" \
  --build-arg VLLM_VERSION="${VLLM_VERSION}" \
  -t "${IMAGE_TAG}" .

# Build proxy image
IMAGE_TAG="miner-api-proxy"
DOCKERFILE="services/miner-api/proxy.Dockerfile"

echo "🔨 Building Docker image ${IMAGE_TAG} with ${DOCKERFILE}..."
sudo DOCKER_BUILDKIT=1 docker build \
  -f "${DOCKERFILE}" \
  -t "${IMAGE_TAG}" .

echo "✅ Build complete: vllm-miner-api-backend and miner-api-proxy"