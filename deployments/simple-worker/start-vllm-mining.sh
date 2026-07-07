#!/usr/bin/env bash
set -e

# Mining vLLM sidecar — loads the chain-registered mining model on the SAME
# GPU as the primary inference model, so a confidential worker can keep its
# big inference model resident (port 8000) while a second, smaller instance
# (this one) generates PoW proofs the chain will actually accept. Gated off
# by default so single-model deployments are unaffected.
if [ "${MINING_VLLM_ENABLED:-false}" != "true" ]; then
  echo "[vLLM-mining] MINING_VLLM_ENABLED != true — mining instance disabled; exiting."
  exit 0
fi

: ${MINING_MODEL_NAME:=Qwen/Qwen3-8B}
: ${MINING_MODEL_COMMIT:=}
: ${MINING_MAX_MODEL_LEN:=8192}
: ${MINING_GPU_MEM_UTIL:=0.26}
: ${MINING_VLLM_PORT:=8001}
: ${API_KEY:=internal-secret}

# PoW sampler wiring — broker egress to the SAME ProofCollector as the
# primary instance (the single miner-proxy fronts both backends).
# POW_PROXY_ENABLE must be falsy in broker mode (PowEgressConfigError).
# cpp proof processor for hashrate parity with the k8 mining fleet.
export VLLM_ENABLE_POW=1
export POW_EGRESS_MODE=broker
export POW_PROXY_ENABLE=false
export ZMQ_PUSH_HOST=127.0.0.1
export ZMQ_PUSH_PORT=${PROOF_COLLECTOR_PORT:-7002}
export POW_PROCESSOR_MODE=${MINING_POW_PROCESSOR_MODE:-cpp}
# Required for the miner-proxy's background-response dummy pool: dummies
# drive POST /v1/responses with store=True/background=True, which vLLM
# rejects (400) unless this is set. The k8 mining fleet sets it too.
export VLLM_ENABLE_RESPONSES_API_STORE=1

# Serialize GPU memory profiling: vLLM sizes its KV cache against free VRAM
# at startup, so this instance must not profile until the primary has
# finished allocating. GPU_MEM_UTIL (primary) + MINING_GPU_MEM_UTIL are
# fractions of TOTAL VRAM and must leave headroom for both CUDA contexts
# (~2-3 GB combined) — keep the sum at or below ~0.86 on an 80 GB card.
PRIMARY_URL="http://127.0.0.1:8000"
echo "[vLLM-mining] Waiting for primary vLLM at ${PRIMARY_URL} to allocate first..."
RETRY=0
while [ $RETRY -lt 360 ]; do
  if curl -s "${PRIMARY_URL}/health" > /dev/null 2>&1; then
    echo "[vLLM-mining] Primary vLLM is up; starting mining instance."
    break
  fi
  RETRY=$((RETRY + 1))
  sleep 5
done
if [ $RETRY -eq 360 ]; then
  echo "[vLLM-mining] ERROR: primary vLLM never became healthy; refusing to allocate."
  exit 1
fi

echo "[vLLM-mining] Starting: $MINING_MODEL_NAME (max_length: $MINING_MAX_MODEL_LEN, util: $MINING_GPU_MEM_UTIL, port: $MINING_VLLM_PORT)"
echo "[vLLM-mining] PoW publishing to tcp://${ZMQ_PUSH_HOST}:${ZMQ_PUSH_PORT} (processor: ${POW_PROCESSOR_MODE})"

# Mining-only recipe — mirrors the k8 tensorcash-simple-worker module
# (Qwen3-8B): no tool-call parser or chat-template overrides; traffic is
# the miner-proxy's dummy mining requests plus optional plaintext Qwen3-8B
# inference dispatched by the broker.
VLLM_CMD="vllm serve $MINING_MODEL_NAME \
  --served-model-name $MINING_MODEL_NAME \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --max-num-seqs 32 \
  --host 0.0.0.0 \
  --port $MINING_VLLM_PORT \
  --api-key ${API_KEY} \
  --download-dir /models/hub \
  --load-format safetensors \
  --max-model-len $MINING_MAX_MODEL_LEN \
  --enable-prompt-tokens-details \
  --generation-config vllm \
  --gpu-memory-utilization $MINING_GPU_MEM_UTIL"

# Pin the snapshot — the PoW model hash is bound to this exact revision on
# chain, and offline (HF_HUB_OFFLINE=1) workers resolve from the local
# cache seeded at bake time.
if [ -n "$MINING_MODEL_COMMIT" ]; then
  VLLM_CMD="$VLLM_CMD --revision $MINING_MODEL_COMMIT"
  echo "[vLLM-mining] Using model commit: $MINING_MODEL_COMMIT"
fi

echo "[vLLM-mining] Executing: $VLLM_CMD"
# Run (NOT exec) so we can force a non-zero exit if the ENABLED mining vLLM
# dies. supervisord runs this program with autorestart=unexpected +
# exitcodes=0, so a clean vLLM shutdown (exit 0) is treated as "intentionally
# disabled" and the 8B is NOT restarted — which silently kills mining until
# the next image recreate. Forcing exit 1 here makes supervisord restart it.
# (The disabled case returns exit 0 at the top of this script, before here, so
# it still correctly stays down when MINING_VLLM_ENABLED != true.)
$VLLM_CMD
RC=$?
echo "[vLLM-mining] mining vLLM exited (code $RC) while enabled; exiting non-zero so supervisord restarts it"
exit 1
