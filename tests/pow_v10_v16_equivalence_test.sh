#!/usr/bin/env bash
# PoW Equivalency Test: v0.10 vs v0.16
#
# Run on GPU node (<gpu-host>).
# Starts each vLLM image in turn, sends identical PoW requests,
# captures proofs to disk, then compares them field-by-field.
#
# Prerequisites:
#   - Docker with nvidia runtime
#   - Both images available (old v0.10, new v0.16)
#   - Model weights cached at ./models
#
# Usage:
#   bash pow_v10_v16_equivalence_test.sh [--build-v16] [--model MODEL] [--old-image IMAGE] [--new-image IMAGE]

set -euo pipefail

# ──────────── Configuration ────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MODEL="${MODEL:-Qwen/Qwen3-8B}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-1024}"
MAX_TOKENS=256
NUM_REQUESTS=2
API_KEY="equiv-test-key"
CONTAINER_NAME_OLD="pow-equiv-v010"
CONTAINER_NAME_NEW="pow-equiv-v016"

# Image tags
OLD_IMAGE="${OLD_IMAGE:-ghcr.io/tensorcash/vllm-backend:cuda12.8.0-vllm0.10.0}"
NEW_IMAGE="${NEW_IMAGE:-tensorcash/vllm-backend:cuda12.3.0-vllm0.16.0-pow}"

# Directories for proof capture
WORK_DIR="/tmp/pow-equiv-test-$$"
OLD_PROOFS="$WORK_DIR/proofs-v010"
NEW_PROOFS="$WORK_DIR/proofs-v016"

BUILD_V16=false

# ──────────── Parse args ────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --build-v16)   BUILD_V16=true; shift ;;
    --model)       MODEL="$2"; shift 2 ;;
    --old-image)   OLD_IMAGE="$2"; shift 2 ;;
    --new-image)   NEW_IMAGE="$2"; shift 2 ;;
    --max-tokens)  MAX_TOKENS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--build-v16] [--model M] [--old-image I] [--new-image I]"
      exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ──────────── Helpers ────────────
cleanup() {
  echo "[cleanup] Stopping containers..."
  docker rm -f "$CONTAINER_NAME_OLD" "$CONTAINER_NAME_NEW" 2>/dev/null || true
}
trap cleanup EXIT

wait_for_health() {
  local name="$1" timeout="${2:-300}"
  echo "[wait] Waiting for $name to be healthy (up to ${timeout}s)..."
  for i in $(seq 1 "$timeout"); do
    if curl -sf "http://localhost:8000/health" >/dev/null 2>&1; then
      echo "[wait] $name healthy after ${i}s"
      return 0
    fi
    sleep 1
  done
  echo "[FAIL] $name did not become healthy in ${timeout}s"
  docker logs "$name" --tail 50
  return 1
}

# Fixed PoW parameters — identical for both runs
BLOCK_HASH="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
VDF="00$(python3 -c "print('ab'*128)")00000001$(python3 -c "print('cd'*128)")"
HEADER_PREFIX="$(python3 -c "print(b'fixed-header-for-equiv-test-00000000000000000000000000000000'.hex())")"
TICK=44
TARGET="fff972474538f000000000000000000000000000000000000000000000000000"

# Fixed prompts (deterministic)
PROMPT_1="List the benefits of dual proof of work scheme that allow to secure a blockchain while running AI inference"
PROMPT_2="Explain how blockchain technology can enhance AI model verification and trust"

echo "============================================"
echo " PoW Equivalency Test: v0.10 vs v0.16"
echo "============================================"
echo "Model:       $MODEL"
echo "Max tokens:  $MAX_TOKENS"
echo "Requests:    $NUM_REQUESTS"
echo "Old image:   $OLD_IMAGE"
echo "New image:   $NEW_IMAGE"
echo "Work dir:    $WORK_DIR"
echo ""

# ──────────── Step 0: Build v0.16 image if requested ────────────
if $BUILD_V16; then
  echo "[build] Building v0.16 PoW image..."
  docker build \
    -t "$NEW_IMAGE" \
    -f "$REPO_ROOT/services/miner-api/vllm_v016.Dockerfile" \
    --build-arg CUDA_VERSION=12.8.0 \
    --build-arg VLLM_VERSION=0.16.0 \
    "$REPO_ROOT"
  echo "[build] Done."
fi

mkdir -p "$OLD_PROOFS" "$NEW_PROOFS"

# ──────────── Helper: send requests ────────────
# $1 = label, $2 = proof_dir, $3 = api_field ("extra_sampling_params" or "vllm_xargs")
send_requests() {
  local label="$1" proof_dir="$2" api_field="$3"

  echo "[$label] Sending $NUM_REQUESTS requests (max_tokens=$MAX_TOKENS)..."

  for i in $(seq 1 "$NUM_REQUESTS"); do
    local prompt
    if [ "$i" -eq 1 ]; then prompt="$PROMPT_1"; else prompt="$PROMPT_2"; fi

    local payload
    payload=$(python3 -c "
import json
req = {
    'model': '$MODEL',
    'prompt': ['$prompt'],
    'max_tokens': $MAX_TOKENS,
    'temperature': 0.95,
    'top_k': 30,
    'top_p': 1.0,
    '$api_field': {
        'pow': {
            'block_hash': '$BLOCK_HASH',
            'vdf': '$VDF',
            'tick': $TICK,
            'target': '$TARGET',
            'header_prefix': '$HEADER_PREFIX',
            'request_id': $i,
            'difficulty': '$TARGET'
        }
    }
}
print(json.dumps(req))
")

    echo "[$label]   Request $i/$NUM_REQUESTS..."
    local resp
    resp=$(curl -sf -X POST "http://localhost:8000/v1/completions" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $API_KEY" \
      -d "$payload" 2>&1) || {
        echo "[$label]   FAILED: $resp"
        return 1
      }

    # Show token count from response
    local tokens
    tokens=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('usage',{}).get('completion_tokens','?'))" 2>/dev/null || echo "?")
    echo "[$label]   OK — $tokens completion tokens"

    # Brief pause between requests
    sleep 1
  done

  # Wait for proof flush
  echo "[$label] Waiting 5s for proof disk flush..."
  sleep 5

  # Copy proofs from container
  echo "[$label] Copying proofs from container..."
  local container="$4"
  docker cp "$container:/data/pow_proofs/." "$proof_dir/" 2>/dev/null || {
    echo "[$label]   WARNING: /data/pow_proofs not found, trying /data/miner_logs/"
    docker cp "$container:/data/miner_logs/." "$proof_dir/" 2>/dev/null || true
  }

  local count
  count=$(find "$proof_dir" -name "*.bin" 2>/dev/null | wc -l)
  echo "[$label] Captured $count proof files"
}

# ──────────── Step 1: Run old v0.10 image ────────────
echo ""
echo "===== Phase 1: v0.10 (old) ====="
cleanup 2>/dev/null || true

echo "[v0.10] Starting container..."
docker run -d \
  --name "$CONTAINER_NAME_OLD" \
  --runtime=nvidia \
  --gpus all \
  -p 8000:8000 \
  -v ./models:/models:rw \
  -e MODEL_NAME="$MODEL" \
  -e MAX_MODEL_LEN="$MAX_MODEL_LEN" \
  -e API_KEY="$API_KEY" \
  -e VLLM_ENABLE_POW=1 \
  -e POW_SAVE_TO_DISK=true \
  -e ZMQ_PUSH_HOST=localhost \
  -e ZMQ_PUSH_PORT=7000 \
  -e CC=/usr/bin/x86_64-linux-gnu-gcc-11 \
  --user root \
  --entrypoint bash \
  "$OLD_IMAGE" \
  -c "rm -f /usr/local/lib/libgmp.so /usr/local/lib/libgmp.so.10 /usr/local/lib/libgmp.so.10.5.0 && ldconfig && exec /app/start.sh"

wait_for_health "$CONTAINER_NAME_OLD"
send_requests "v0.10" "$OLD_PROOFS" "extra_sampling_params" "$CONTAINER_NAME_OLD"

echo "[v0.10] Stopping container..."
docker stop "$CONTAINER_NAME_OLD" >/dev/null
docker rm "$CONTAINER_NAME_OLD" >/dev/null

# ──────────── Step 2: Run new v0.16 image ────────────
echo ""
echo "===== Phase 2: v0.16 (new) ====="

echo "[v0.16] Starting container..."
docker run -d \
  --name "$CONTAINER_NAME_NEW" \
  --runtime=nvidia \
  --gpus all \
  -p 8000:8000 \
  -v ./models:/models:rw \
  -e MODEL_NAME="$MODEL" \
  -e MAX_MODEL_LEN="$MAX_MODEL_LEN" \
  -e API_KEY="$API_KEY" \
  -e VLLM_ENABLE_POW=1 \
  -e POW_SAVE_TO_DISK=true \
  -e ZMQ_PUSH_HOST=localhost \
  -e ZMQ_PUSH_PORT=7000 \
  -e CC=/usr/bin/x86_64-linux-gnu-gcc-11 \
  --user root \
  --entrypoint bash \
  "$NEW_IMAGE" \
  -c "rm -f /usr/local/lib/libgmp.so /usr/local/lib/libgmp.so.10 /usr/local/lib/libgmp.so.10.5.0 && ldconfig && exec python3 -m vllm.entrypoints.openai.api_server --model $MODEL --trust-remote-code --tensor-parallel-size 1 --max-num-seqs 32 --host 0.0.0.0 --port 8000 --api-key $API_KEY --download-dir /models/hub --load-format safetensors --max-model-len $MAX_MODEL_LEN --gpu-memory-utilization 0.8"

wait_for_health "$CONTAINER_NAME_NEW"
send_requests "v0.16" "$NEW_PROOFS" "vllm_xargs" "$CONTAINER_NAME_NEW"

echo "[v0.16] Stopping container..."
docker stop "$CONTAINER_NAME_NEW" >/dev/null
docker rm "$CONTAINER_NAME_NEW" >/dev/null

# ──────────── Step 3: Compare proofs ────────────
echo ""
echo "===== Phase 3: Comparison ====="

OLD_COUNT=$(find "$OLD_PROOFS" -name "*.bin" | wc -l)
NEW_COUNT=$(find "$NEW_PROOFS" -name "*.bin" | wc -l)

echo "Old proofs: $OLD_COUNT files in $OLD_PROOFS"
echo "New proofs: $NEW_COUNT files in $NEW_PROOFS"

if [ "$OLD_COUNT" -eq 0 ] || [ "$NEW_COUNT" -eq 0 ]; then
  echo "FAIL: Missing proof files — cannot compare."
  echo ""
  echo "Debug: check POW_SAVE_TO_DISK is working in both images."
  echo "  Old proofs dir: $OLD_PROOFS"
  echo "  New proofs dir: $NEW_PROOFS"
  exit 1
fi

# Try the full FlatBuffer comparison first (requires pfunpack)
EQUIV_SCRIPT="$REPO_ROOT/shared-utils/pow-utils/tests/check_pow_ab_equivalence.py"
if [ -f "$EQUIV_SCRIPT" ]; then
  echo ""
  echo "Running full FlatBuffer proof comparison..."
  python3 "$EQUIV_SCRIPT" \
    --old-dir "$OLD_PROOFS" \
    --new-dir "$NEW_PROOFS" \
    --atol 1e-7 \
    --rtol 0.0 \
    --ignore-timestamp \
    --dump-summary-json "$WORK_DIR/equiv-summary.json"
  RESULT=$?

  if [ $RESULT -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo " PASS: v0.10 and v0.16 proofs are equivalent"
    echo "=========================================="
  else
    echo ""
    echo "=========================================="
    echo " FAIL: Proof differences detected"
    echo "=========================================="
    echo "Summary: $WORK_DIR/equiv-summary.json"
  fi
  exit $RESULT
else
  echo ""
  echo "WARNING: check_pow_ab_equivalence.py not found at $EQUIV_SCRIPT"
  echo "Falling back to byte-level comparison..."
  echo ""

  # Simple byte comparison as fallback
  # Sort files by name for consistent ordering
  OLD_FILES=($(find "$OLD_PROOFS" -name "*.bin" | sort))
  NEW_FILES=($(find "$NEW_PROOFS" -name "*.bin" | sort))

  if [ "${#OLD_FILES[@]}" -ne "${#NEW_FILES[@]}" ]; then
    echo "FAIL: Different number of proof files: ${#OLD_FILES[@]} vs ${#NEW_FILES[@]}"
    exit 1
  fi

  ALL_MATCH=true
  for i in "${!OLD_FILES[@]}"; do
    OLD_SIZE=$(stat -c%s "${OLD_FILES[$i]}" 2>/dev/null || stat -f%z "${OLD_FILES[$i]}")
    NEW_SIZE=$(stat -c%s "${NEW_FILES[$i]}" 2>/dev/null || stat -f%z "${NEW_FILES[$i]}")
    if [ "$OLD_SIZE" -ne "$NEW_SIZE" ]; then
      echo "SIZE MISMATCH: $(basename "${OLD_FILES[$i]}") ($OLD_SIZE) vs $(basename "${NEW_FILES[$i]}") ($NEW_SIZE)"
      ALL_MATCH=false
    fi
  done

  if $ALL_MATCH; then
    echo "All proof files have matching sizes (byte-level check only)."
    echo "For full field-level comparison, build pfunpack and re-run."
  else
    echo "FAIL: Size mismatches detected."
    exit 1
  fi
fi

echo ""
echo "Proof files preserved at: $WORK_DIR"
