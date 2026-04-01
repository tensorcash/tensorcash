#!/usr/bin/env bash
set -euo pipefail

# Resolve script location and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Default values
DEFAULT_CU="cu123"
CU_VERSION=""
PROCESSED_DATA_DIR=""
MODELS_DIR=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [-d DATA_DIR] [-c CU_VERSION] [-m MODELS_DIR]
  -d  path to processed-data directory (default: /data)
  -c  CUDA version prefix: cu120 | cu123 | cu126 (default: auto-detect or cu123)
  -m  path to model-data directory (default: ./models)
EOF
  exit 1
}

# Parse options
while getopts "d:c:m:h" opt; do
  case "$opt" in
    d) PROCESSED_DATA_DIR="$OPTARG" ;;
    c) CU_VERSION="$OPTARG" ;;
    m) MODELS_DIR="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done
shift $((OPTIND-1))

# Resolve processed-data dir
if [[ -z "$PROCESSED_DATA_DIR" ]] || [[ ! -d "$PROCESSED_DATA_DIR" ]]; then
  PROCESSED_DATA_DIR="/data"
fi

# Resolve models dir
if [[ -z "$MODELS_DIR" ]] || [[ ! -d "$MODELS_DIR" ]]; then
  MODELS_DIR="./models"
fi

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

# Determine CU_VERSION
if [[ -n "$CU_VERSION" ]]; then
  # user override, validate
  if [[ "$CU_VERSION" =~ ^cu(120|123|126)$ ]]; then
    :
  else
    echo "⚠️  Invalid CUDA prefix '$CU_VERSION'; must be cu120, cu123, or cu126."
    exit 1
  fi
else
  # try auto-detect from nvidia-smi
  if command -v nvidia-smi >/dev/null 2>&1; then
    # first, try the NVML --query-gpu API
    if DETECT_RAW="$(nvidia-smi --query-gpu=cuda_version --format=csv,noheader,nounits 2>/dev/null)"; then
      # take first line (in case of multiple GPUs)
      DETECT_VER="$(echo "$DETECT_RAW" | head -n1)"
    else
      # fallback: parse the summary line
      DETECT_VER="$(nvidia-smi | grep -i 'CUDA Version:' \
        | sed -E 's/.*CUDA Version: *([0-9]+\.[0-9]+).*/\1/' \
        | head -n1 || true)"
    fi
    
    if [[ -n "$DETECT_VER" ]]; then
      # drop the dot, e.g. 12.3 → 123
      DETECT_SHORT="${DETECT_VER/./}"
      CU_VERSION="cu${DETECT_SHORT}"
      # only keep known prefixes
      if [[ ! "$CU_VERSION" =~ ^cu(120|123|126)$ ]]; then
        CU_VERSION="$DEFAULT_CU"
      fi
    else
      CU_VERSION="$DEFAULT_CU"
    fi
  else
    CU_VERSION="$DEFAULT_CU"
  fi
fi

# Map to CUDA version
CUDA_VERSION=$(map_cu_to_cuda "$CU_VERSION")

echo "🔧 Using CUDA build: ${CU_VERSION} (CUDA ${CUDA_VERSION})"
echo "📂 Processed-data directory: ${PROCESSED_DATA_DIR}"

# Hard-coded test command
CONTAINER_CMD=(
  "python" "tests/test.py"
)

# Docker settings
IMAGE_TAG="verification-api"
CONTAINER_NAME="verification-api"
DOCKERFILE="services/verification-api/generic.Dockerfile"

# Build with BuildKit
echo "🔨 Building Docker image ${IMAGE_TAG} using ${DOCKERFILE}..."
sudo DOCKER_BUILDKIT=1 docker build \
  -f "${DOCKERFILE}" \
  --build-arg CUDA_VERSION="${CUDA_VERSION}" \
  -t "${IMAGE_TAG}" .

# Run with GPU support, mounting your data, and executing the hard-coded test
echo "🚀 Running container ${CONTAINER_NAME}, mounting '${PROCESSED_DATA_DIR}', executing: ${CONTAINER_CMD[*]}"
sudo docker run --gpus all -it --rm \
  --name "${CONTAINER_NAME}" \
  -v "${PROCESSED_DATA_DIR}:/data:rw" \
  -v "${MODELS_DIR}:/models:rw" \
  -w /app/src \
  "${IMAGE_TAG}" "${CONTAINER_CMD[@]}"

  # -e CUDA_VISIBLE_DEVICES= \

# example usage "cd /scripts/tests && ./proof_verifier.sh"