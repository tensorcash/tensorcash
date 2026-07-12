#!/usr/bin/env bash
set -e

# Operator review API: defaults to loopback-only inside the container (no key
# needed). To expose it on the published port, set BOTH:
#   export OPERATOR_API_KEY="<secret>"
#   export OPERATOR_HTTP_BIND=0.0.0.0
# verification-api refuses to start with a non-loopback bind and no key.

# WORKER_* / BROKER_* / *_TOKEN: compute broker worker env (defaults keep standalone mode)
sudo API_KEY=${API_KEY:-super-secret-token} \
MODEL_API_KEY=${MODEL_API_KEY:-super-secret-token} \
CUDA_VERSION=${CUDA_VERSION:-12.8.0} \
POW_SAVE_TO_DISK=${POW_SAVE_TO_DISK:-1} \
VLLM_VERSION=${VLLM_VERSION:-0.10.0} \
GUI_MODE=${GUI_MODE:-true} \
WORKER_MODE=${WORKER_MODE:-standalone} \
BROKER_WS_URL=${BROKER_WS_URL:-ws://localhost:8003/v1/ws} \
PROVIDER_JWT_TOKEN=${PROVIDER_JWT_TOKEN:-} \
X_WORKER_TOKEN=${X_WORKER_TOKEN:-} \
WORKER_CAPACITY=${WORKER_CAPACITY:-4} \
COMPUTE_TYPE=${COMPUTE_TYPE:-nvidia-8.6} \
GPU_MODEL=${GPU_MODEL:-A100-80GB} \
GPU_MEMORY_GB=${GPU_MEMORY_GB:-80} \
WORKER_REGION=${WORKER_REGION:-us-west-2} \
MAX_CONTEXT_WINDOW=${MAX_CONTEXT_WINDOW:-128000} \
CHALLENGE_SECRET=${CHALLENGE_SECRET:-} \
OPERATOR_API_KEY=${OPERATOR_API_KEY:-} \
OPERATOR_HTTP_BIND=${OPERATOR_HTTP_BIND:-127.0.0.1} \
docker compose -f deployments/docker-compose/core-miner-validation-api/docker-compose.yaml up --build

# GENESIS_GENERATOR=True \
# POW_PROCESSOR_MODE=python \

