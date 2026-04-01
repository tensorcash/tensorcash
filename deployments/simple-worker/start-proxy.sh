#!/usr/bin/env bash
set -e

# Wait for vLLM to be ready
echo "[Miner-Proxy] Waiting for vLLM to be ready at ${TARGET_URL:-http://127.0.0.1:8000}..."
VLLM_URL="${TARGET_URL:-http://127.0.0.1:8000}"

MAX_RETRIES=60
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
  if curl -s "${VLLM_URL}/health" > /dev/null 2>&1; then
    echo "[Miner-Proxy] vLLM is ready!"
    break
  fi
  RETRY_COUNT=$((RETRY_COUNT + 1))
  echo "[Miner-Proxy] Waiting for vLLM... ($RETRY_COUNT/$MAX_RETRIES)"
  sleep 5
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
  echo "[Miner-Proxy] WARNING: vLLM health check timed out, starting anyway..."
fi

# Dual-backend mode (MINING_VLLM_ENABLED=true): the container-wide
# MODEL_NAME/MODEL_COMMIT configure the PRIMARY (inference) vLLM, but the
# proxy's PoW pinning must point at the chain-registered MINING model.
# Re-export here (per-process) and publish the model→backend routing so
# mining-model requests reach the mining instance and everything else
# gets audit-mode injection against the primary instance.
if [ "${MINING_VLLM_ENABLED:-false}" = "true" ]; then
  if [ -z "${MINING_MODEL_NAME:-}" ] || [ -z "${MINING_MODEL_COMMIT:-}" ]; then
    echo "[Miner-Proxy] ERROR: MINING_VLLM_ENABLED=true requires MINING_MODEL_NAME and MINING_MODEL_COMMIT"
    exit 1
  fi
  PRIMARY_MODEL_NAME="${MODEL_NAME:-}"
  PRIMARY_MODEL_COMMIT="${MODEL_COMMIT:-}"
  export MODEL_NAME="${MINING_MODEL_NAME}"
  export MODEL_COMMIT="${MINING_MODEL_COMMIT}"
  if [ -z "${MODEL_ROUTES:-}" ] && [ -n "$PRIMARY_MODEL_NAME" ]; then
    export MODEL_ROUTES="${PRIMARY_MODEL_NAME}@${PRIMARY_MODEL_COMMIT}=${TARGET_URL:-http://127.0.0.1:8000},${MINING_MODEL_NAME}@${MINING_MODEL_COMMIT}=http://127.0.0.1:${MINING_VLLM_PORT:-8001}"
  fi
  echo "[Miner-Proxy] Dual-backend mode: mining pin ${MODEL_NAME}@${MODEL_COMMIT}"
  echo "[Miner-Proxy] MODEL_ROUTES: ${MODEL_ROUTES}"
fi

# Validate broker configuration
if [ -z "$BROKER_WS_URL" ]; then
  echo "[Miner-Proxy] ERROR: BROKER_WS_URL is not set!"
  echo "[Miner-Proxy] Set BROKER_WS_URL to your broker's WebSocket endpoint (e.g., wss://broker.example.com/v1/ws)"
  exit 1
fi

if [ -z "$PROVIDER_JWT_TOKEN" ]; then
  echo "[Miner-Proxy] ERROR: PROVIDER_JWT_TOKEN is not set!"
  echo "[Miner-Proxy] Set PROVIDER_JWT_TOKEN to your JWT authentication token from the broker"
  exit 1
fi

echo "[Miner-Proxy] Configuration:"
echo "  WORKER_MODE: ${WORKER_MODE:-broker}"
echo "  BROKER_WS_URL: ${BROKER_WS_URL}"
echo "  TARGET_URL: ${TARGET_URL:-http://127.0.0.1:8000}"
echo "  WORKER_CAPACITY: ${WORKER_CAPACITY:-4}"
echo "  GPU_MODEL: ${GPU_MODEL:-unknown}"
echo "  MINING_ENABLED: ${MINING_ENABLED:-true}"

cd /app/miner-proxy/src
exec python main.py
