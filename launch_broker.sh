#!/usr/bin/env bash
# Launch script for connecting to compute.tensorcash.org as a worker
# Usage: ./launch_broker.sh
#
# Before running, set your JWT token:
#   export PROVIDER_JWT_TOKEN="eyJh..."
#
set -e

# Check if JWT token is set
if [ -z "$PROVIDER_JWT_TOKEN" ]; then
    echo "ERROR: PROVIDER_JWT_TOKEN environment variable is not set"
    echo ""
    echo "Please set your JWT token before running:"
    echo "  export PROVIDER_JWT_TOKEN=\"eyJh...\""
    echo ""
    echo "Or run with inline:"
    echo "  PROVIDER_JWT_TOKEN=\"eyJh...\" ./launch_broker.sh"
    exit 1
fi

echo "=============================================="
echo " TensorCash Miner - Compute Broker Mode"
echo "=============================================="
echo "Broker URL: wss://compute.tensorcash.org/v1/ws"
echo "Region: eu-west-2"
echo "GPU: Titan RTX (24GB)"
echo "JWT Token: ${PROVIDER_JWT_TOKEN:0:20}..."
echo "=============================================="
echo ""

sudo \
    API_KEY=super-secret-token \
    MAX_MODEL_LEN=12000 \
    MODEL_API_KEY=super-secret-token \
    CUDA_VERSION=12.8.0 \
    POW_SAVE_TO_DISK=1 \
    VLLM_VERSION=0.10.0 \
    GUI_MODE=true \
    \
    WORKER_MODE=broker \
    BROKER_WS_URL=wss://compute.tensorcash.org/v1/ws \
    PROVIDER_JWT_TOKEN="$PROVIDER_JWT_TOKEN" \
    \
    WORKER_CAPACITY=32 \
    COMPUTE_TYPE=nvidia-7.5 \
    GPU_MODEL=Titan-RTX \
    GPU_MEMORY_GB=24 \
    WORKER_REGION=eu-west-2 \
    MAX_CONTEXT_WINDOW=32768 \
    \
docker compose -f deployments/docker-compose/core-miner-validation-api/docker-compose.yaml up --build -d

