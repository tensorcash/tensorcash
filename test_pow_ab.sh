#!/usr/bin/env bash
#
# PoW A/B test: build OLD and NEW llama.cpp images, run deterministic
# PoW requests against each, then compare proof outputs.
#
# Usage: cd /path/to/tensorcash && bash test_pow_ab.sh
#
set -euo pipefail

LLAMA_DIR="services/miner-api/llama.cpp"
OLD_REF="a97806c856cf4b6e303a75d0b1fae82e957cdff9"  # pow_sampler_complid
NEW_REF="pow_upstream_report"
IMAGE_OLD="pow-ab-old:test"
IMAGE_NEW="pow-ab-new:test"
PROOF_OLD="/tmp/pow-old"
PROOF_NEW="/tmp/pow-new"
CONTAINER_OLD="pow-ab-old"
CONTAINER_NEW="pow-ab-new"
PORT_OLD=18000
PORT_NEW=18001
MAX_WAIT=300  # seconds to wait for server health

DOCKERFILE="deployments/simple-worker-cpu/Dockerfile"

# Fixed PoW request payload for deterministic comparison
POW_REQUEST='{
  "model": "test",
  "prompt": "The quick brown fox jumps over the lazy dog. Once upon a time in a land far away, there lived a wise old wizard who knew the secrets of the universe.",
  "max_tokens": 512,
  "temperature": 0.8,
  "top_k": 40,
  "top_p": 0.95,
  "seed": 42,
  "stream": false,
  "extra_sampling_params": {
    "pow": {
      "block_hash": "0000000000000000000000000000000000000000000000000000000000000001",
      "vdf": "",
      "tick": 1,
      "target": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "header_prefix": "00000000000000000000000000000000000000000000000000000000000000000000000000000000",
      "ipfs_cid": "QmTestCid",
      "request_id": 1001,
      "difficulty": 1,
      "model_identifier": "Qwen/Qwen2.5-0.5B-Instruct@test",
      "compute_precision": "fp16"
    }
  }
}'

cleanup() {
    echo "[cleanup] Stopping containers..."
    docker rm -f "$CONTAINER_OLD" "$CONTAINER_NEW" 2>/dev/null || true
    # Restore NEW state
    (cd "$LLAMA_DIR" && git checkout "$NEW_REF" --quiet 2>/dev/null || true)
}
trap cleanup EXIT

# Patch Dockerfile to be arm64-compatible:
# Replace the x86-only flatc binary download with COPY from builder stage.
patch_dockerfile_arm64() {
    local df="$1"
    # Replace x86_64 flatc download with COPY from llama-builder
    if grep -q "Linux.flatc.binary" "$df"; then
        sed -i.bak '/# Install FlatBuffers binary/,/rm Linux.flatc.binary/c\
# Copy FlatBuffers binary + libs from builder (arch-independent)\
COPY --from=llama-builder /usr/local/bin/flatc /usr/local/bin/flatc\
COPY --from=llama-builder /usr/local/lib/libflatbuffers* /usr/local/lib/\
RUN ldconfig' "$df"
        rm -f "${df}.bak"
    fi
}

wait_for_health() {
    local container="$1"
    local port="$2"
    local start=$SECONDS
    echo "[wait] Waiting for $container on port $port..."
    while true; do
        if docker exec "$container" curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
            echo "[wait] $container is healthy (took $((SECONDS - start))s)"
            return 0
        fi
        if (( SECONDS - start > MAX_WAIT )); then
            echo "[FAIL] $container did not become healthy in ${MAX_WAIT}s"
            echo "[FAIL] Last 50 lines of container logs:"
            docker logs "$container" --tail 50
            return 1
        fi
        sleep 5
    done
}

send_pow_request() {
    local port="$1"
    local label="$2"
    echo "[$label] Sending PoW completion request on port $port..."
    local resp
    resp=$(curl -sf --max-time 120 -X POST "http://127.0.0.1:${port}/v1/completions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer internal-secret" \
        -d "$POW_REQUEST" 2>&1) || {
        echo "[$label] Request failed. Response: $resp"
        return 1
    }
    echo "[$label] Got response (first 200 chars): ${resp:0:200}"
}

# ─────────────────────────────────────────────────────────────────────────────
echo "============================================"
echo " PoW A/B Test (native $(uname -m))"
echo "============================================"
echo ""

# Clean up
rm -rf "$PROOF_OLD" "$PROOF_NEW"
mkdir -p "$PROOF_OLD" "$PROOF_NEW"
docker rm -f "$CONTAINER_OLD" "$CONTAINER_NEW" 2>/dev/null || true

# Save current Dockerfile (new version)
cp "$DOCKERFILE" "${DOCKERFILE}.new-save"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Build OLD image
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo ">>> Phase 1: Build OLD image"
echo ""

echo "[OLD] Checking out old llama.cpp: ${OLD_REF:0:12}"
(cd "$LLAMA_DIR" && git checkout "$OLD_REF" --quiet)

# Restore original Dockerfile, then patch for arm64
git checkout -- "$DOCKERFILE"
patch_dockerfile_arm64 "$DOCKERFILE"

echo "[OLD] Building Docker image (native arm64, no QEMU)..."
time docker buildx build \
    --load \
    -f "$DOCKERFILE" \
    -t "$IMAGE_OLD" \
    . 2>&1 | tail -15

echo "[OLD] Running container..."
docker run -d \
    --name "$CONTAINER_OLD" \
    -p "${PORT_OLD}:8000" \
    -e "PROOF_SAVE_DIR=/tmp/pow_proofs" \
    -e "MAX_MODEL_LEN=1024" \
    -e "LLAMA_PARALLEL=1" \
    -v "${PROOF_OLD}:/tmp/pow_proofs" \
    "$IMAGE_OLD"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Build NEW image (in parallel with old container startup)
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo ">>> Phase 2: Build NEW image"
echo ""

echo "[NEW] Checking out new llama.cpp: $NEW_REF"
(cd "$LLAMA_DIR" && git checkout "$NEW_REF" --quiet)

# Restore the new Dockerfile + arm64 patch
cp "${DOCKERFILE}.new-save" "$DOCKERFILE"
patch_dockerfile_arm64 "$DOCKERFILE"
rm -f "${DOCKERFILE}.new-save"

echo "[NEW] Building Docker image (native arm64, no QEMU)..."
time docker buildx build \
    --load \
    -f "$DOCKERFILE" \
    -t "$IMAGE_NEW" \
    . 2>&1 | tail -15

# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Wait for OLD, send request, collect proofs
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo ">>> Phase 3: OLD request"
echo ""

wait_for_health "$CONTAINER_OLD" "$PORT_OLD"
send_pow_request "$PORT_OLD" "OLD"

# Give proof writer time to flush
sleep 5

echo "[OLD] Proofs collected:"
ls -la "$PROOF_OLD"/ 2>/dev/null || echo "  (none)"
OLD_PROOF_COUNT=$(find "$PROOF_OLD" -name '*.bin' 2>/dev/null | wc -l | tr -d ' ')
echo "[OLD] Proof .bin files: $OLD_PROOF_COUNT"

docker rm -f "$CONTAINER_OLD" 2>/dev/null

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: Run NEW, send same request, collect proofs
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo ">>> Phase 4: NEW request"
echo ""

echo "[NEW] Running container..."
docker run -d \
    --name "$CONTAINER_NEW" \
    -p "${PORT_NEW}:8000" \
    -e "PROOF_SAVE_DIR=/tmp/pow_proofs" \
    -e "MAX_MODEL_LEN=1024" \
    -e "LLAMA_PARALLEL=1" \
    -v "${PROOF_NEW}:/tmp/pow_proofs" \
    "$IMAGE_NEW"

wait_for_health "$CONTAINER_NEW" "$PORT_NEW"
send_pow_request "$PORT_NEW" "NEW"

sleep 5

echo "[NEW] Proofs collected:"
ls -la "$PROOF_NEW"/ 2>/dev/null || echo "  (none)"
NEW_PROOF_COUNT=$(find "$PROOF_NEW" -name '*.bin' 2>/dev/null | wc -l | tr -d ' ')
echo "[NEW] Proof .bin files: $NEW_PROOF_COUNT"

docker rm -f "$CONTAINER_NEW" 2>/dev/null

# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: Compare proofs
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo " Phase 5: Proof Comparison"
echo "============================================"
echo "OLD proofs: $OLD_PROOF_COUNT files"
echo "NEW proofs: $NEW_PROOF_COUNT files"

if [ "$OLD_PROOF_COUNT" -eq 0 ] || [ "$NEW_PROOF_COUNT" -eq 0 ]; then
    echo ""
    echo "[WARN] Not enough proofs to compare."
    echo "[WARN] OLD dir ($PROOF_OLD): $OLD_PROOF_COUNT files"
    echo "[WARN] NEW dir ($PROOF_NEW): $NEW_PROOF_COUNT files"
    echo ""
    echo "Debug: check container logs with 'docker logs pow-ab-old/pow-ab-new'"
    exit 2
fi

# Build pfunpack if needed
CHECKER_DIR="shared-utils/pow-utils/tests"
if ! python3 -c "import pfunpack" 2>/dev/null; then
    echo "[checker] Building pfunpack..."
    (cd "$CHECKER_DIR" && bash build_pfunpack.sh 2>&1 | tail -5) || {
        echo "[WARN] Failed to build pfunpack. Install deps or build manually."
        echo "  cd $CHECKER_DIR && ./build_pfunpack.sh"
        exit 2
    }
fi

echo ""
echo "[checker] Running A/B equivalence check..."
PYTHONPATH="$CHECKER_DIR:${PYTHONPATH:-}" python3 "$CHECKER_DIR/check_pow_ab_equivalence.py" \
    --old-dir "$PROOF_OLD" \
    --new-dir "$PROOF_NEW" \
    --ignore-timestamp \
    --atol 1e-7 \
    --rtol 0.0 \
    --dump-summary-json /tmp/pow_ab_summary.json

EXIT_CODE=$?
echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "============================================"
    echo " PASS: Old and new proofs are equivalent"
    echo "============================================"
elif [ $EXIT_CODE -eq 1 ]; then
    echo "============================================"
    echo " FAIL: Proof equivalence check failed"
    echo "============================================"
    echo "Summary:"
    python3 -m json.tool /tmp/pow_ab_summary.json 2>/dev/null || cat /tmp/pow_ab_summary.json 2>/dev/null
else
    echo "============================================"
    echo " ERROR: Setup/runtime error (exit=$EXIT_CODE)"
    echo "============================================"
fi

exit $EXIT_CODE
