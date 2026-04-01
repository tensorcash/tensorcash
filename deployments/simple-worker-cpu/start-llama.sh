#!/usr/bin/env bash
set -e

# Get configuration from environment
: ${MODEL_NAME:=Qwen/Qwen2.5-0.5B-Instruct}
: ${MAX_MODEL_LEN:=2048}
: ${API_KEY:=internal-secret}
: ${LLAMA_PARALLEL:=2}

echo "[llama-server] Preparing model: $MODEL_NAME"
if [ -n "$MODEL_COMMIT" ]; then
    echo "[llama-server] Pinned to HuggingFace revision: $MODEL_COMMIT"
fi

# Download and convert model to GGUF
python3 /app/prepare_model.py

# Derive GGUF filename (same logic as prepare_model.py)
MODEL_NAME_CLEAN=$(echo "$MODEL_NAME" | sed -E 's/[^a-zA-Z0-9]+/_/g' | sed -E 's/^_+|_+$//g')
MODEL_FILE="/models/${MODEL_NAME_CLEAN}.gguf"

if [ ! -f "$MODEL_FILE" ]; then
    echo "[llama-server] ERROR: Model file not found: $MODEL_FILE"
    exit 1
fi

echo "[llama-server] Starting with model: $MODEL_FILE"
echo "[llama-server] Context: $MAX_MODEL_LEN, Parallel: $LLAMA_PARALLEL"

# Pick a canonical chat template when the loaded GGUF belongs to a known-broken
# family (community Q4 quants often strip or break tokenizer.chat_template,
# which makes llama.cpp's autoparser fall through and skip the lazy PEG grammar
# that would constrain tool_call JSON — model emits unconstrained garbage).
# Detection mirrors services/miner-api/llama_supervisor.py:resolve_chat_template_file.
CHAT_TEMPLATE_FILE=""
CHAT_TEMPLATE_DIR="${LLAMA_CHAT_TEMPLATE_DIR:-}"
if [ -n "${LLAMA_CHAT_TEMPLATE_FILE:-}" ]; then
    # Explicit override always wins.
    if [ -f "$LLAMA_CHAT_TEMPLATE_FILE" ]; then
        CHAT_TEMPLATE_FILE="$LLAMA_CHAT_TEMPLATE_FILE"
    elif [ -n "$CHAT_TEMPLATE_DIR" ] && [ -f "$CHAT_TEMPLATE_DIR/$LLAMA_CHAT_TEMPLATE_FILE" ]; then
        CHAT_TEMPLATE_FILE="$CHAT_TEMPLATE_DIR/$LLAMA_CHAT_TEMPLATE_FILE"
    fi
elif [ -n "$CHAT_TEMPLATE_DIR" ]; then
    HAYSTACK="$(printf '%s %s' "$MODEL_NAME" "$(basename "$MODEL_FILE")" | tr '[:upper:]' '[:lower:]')"
    case "$HAYSTACK" in
        *hermes*)
            [ -f "$CHAT_TEMPLATE_DIR/hermes.jinja" ] && CHAT_TEMPLATE_FILE="$CHAT_TEMPLATE_DIR/hermes.jinja"
            ;;
    esac
fi

if [ -n "$CHAT_TEMPLATE_FILE" ]; then
    echo "[llama-server] Using chat-template-file: $CHAT_TEMPLATE_FILE"
    exec /usr/local/bin/llama-server \
        -m "$MODEL_FILE" \
        --host 0.0.0.0 \
        --port 8000 \
        --ctx-size "$MAX_MODEL_LEN" \
        --parallel "$LLAMA_PARALLEL" \
        --api-key "$API_KEY" \
        --jinja \
        --chat-template-file "$CHAT_TEMPLATE_FILE"
else
    # Serve on port 8000 (matches TARGET_URL for miner-proxy)
    exec /usr/local/bin/llama-server \
        -m "$MODEL_FILE" \
        --host 0.0.0.0 \
        --port 8000 \
        --ctx-size "$MAX_MODEL_LEN" \
        --parallel "$LLAMA_PARALLEL" \
        --api-key "$API_KEY" \
        --jinja
fi
