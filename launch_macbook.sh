#!/usr/bin/env bash
set -e

# Script to launch docker compose, download llama-server binary, and run it
#
# IMPORTANT: This script requires a GitHub token for private repository access.
# Run the download test script first to verify authentication works:
#   export GITHUB_TOKEN="your_token_here"
#   ./download_test.sh
#
# If download test fails, this script will also fail and leave docker running.

# --- CONFIG ---
DOCKER_COMPOSE_FILE="deployments/docker-compose/core-miner-validation-api/docker-compose_llamacpp_local.yaml"
LLAMA_SERVER_URL="https://github.com/tensorcash/tensorcash/releases/download/v1.0.2/llama-server-darwin-arm64.tar.gz"
LLAMA_SERVER_TAR="llama-server-darwin-arm64.tar.gz"
LLAMA_SERVER_DIR="llama-server-darwin-arm64"
LLAMA_SERVER_BIN="llama-server"
MODEL_FILE="${MODEL_FILE:-$HOME/models/Qwen_Qwen3_8B.gguf}"

# Binary download location
BINARY_DOWNLOAD_DIR="$HOME/models"
BINARY_TAR_PATH="$BINARY_DOWNLOAD_DIR/$LLAMA_SERVER_TAR"
BINARY_DIR_PATH="$BINARY_DOWNLOAD_DIR/$LLAMA_SERVER_DIR"
BINARY_BIN_PATH="$BINARY_DOWNLOAD_DIR/$LLAMA_SERVER_BIN"

# GitHub token for private repo access (set this as environment variable)
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# Minimum expected file size (adjust as needed)
MIN_FILE_SIZE=1048576  # 1MB minimum

# --- FUNCTIONS ---
check_sudo() {
    if [ "$EUID" -ne 0 ]; then
        echo "This script requires sudo privileges. Please run with sudo or as root."
        exit 1
    fi
}

download_with_auth() {
    local url="$1"
    local output="$2"
    
    if [ -z "$GITHUB_TOKEN" ]; then
        echo "ERROR: No GitHub token provided!"
        echo "Private repositories require authentication."
        echo "Set GITHUB_TOKEN environment variable with a valid token."
        echo "Create token at: https://github.com/settings/tokens"
        cleanup
        exit 1
    fi
    
    echo "Using GitHub Assets API for private repo download..."
    
    # Extract repo info from URL
    # URL format: https://github.com/owner/repo/releases/download/tag/filename
    REPO_PATH=$(echo "$url" | sed 's|https://github.com/||' | sed 's|/releases/download/.*||')
    TAG=$(echo "$url" | sed 's|.*/releases/download/||' | sed 's|/.*||')
    FILENAME=$(basename "$url")
    
    echo "Repository: $REPO_PATH"
    echo "Tag: $TAG" 
    echo "Filename: $FILENAME"
    
    # Get release info from API
    API_URL="https://api.github.com/repos/$REPO_PATH/releases/tags/$TAG"
    echo "Fetching release info from: $API_URL"
    
    RELEASE_JSON=$(curl -s -H "Authorization: token $GITHUB_TOKEN" "$API_URL")
    
    if echo "$RELEASE_JSON" | grep -q '"message": "Not Found"'; then
        echo "ERROR: Release not found or no access to release"
        cleanup
        exit 1
    fi
    
    # Extract asset ID for our specific file (FIX: get the asset ID, not uploader ID)
    ASSET_ID=$(echo "$RELEASE_JSON" | grep -B2 "\"$FILENAME\"" | grep '"id":' | head -1 | grep -o '[0-9]\+')
    
    if [ -z "$ASSET_ID" ]; then
        echo "ERROR: Could not find asset ID for $FILENAME"
        echo "Available assets:"
        echo "$RELEASE_JSON" | grep '"name":' | head -5
        cleanup
        exit 1
    fi
    
    echo "Found asset ID: $ASSET_ID"
    
    # Download using Assets API
    ASSET_URL="https://api.github.com/repos/$REPO_PATH/releases/assets/$ASSET_ID"
    echo "Downloading from Assets API: $ASSET_URL"
    
    curl -L \
         -H "Authorization: token $GITHUB_TOKEN" \
         -H "Accept: application/octet-stream" \
         -o "$output" \
         "$ASSET_URL"
    
    # Check if download was successful and file size is reasonable
    if [ ! -f "$output" ]; then
        echo "ERROR: Download failed - file not found"
        cleanup
        exit 1
    fi
    
    file_size=$(stat -f%z "$output" 2>/dev/null || stat -c%s "$output" 2>/dev/null)
    echo "Downloaded file size: $file_size bytes"
    
    if [ "$file_size" -lt "$MIN_FILE_SIZE" ]; then
        echo "ERROR: Downloaded file is too small ($file_size bytes)."
        echo "This indicates an authentication or access error."
        echo ""
        echo "File contents (likely an error message):"
        echo "============================================"
        head -n 10 "$output"
        echo "============================================"
        cleanup
        exit 1
    fi
    
    echo "Successfully downloaded $output ($file_size bytes)"
    
    # Verify it's actually a gzipped tar file
    if ! file "$output" | grep -q "gzip compressed"; then
        echo "WARNING: File doesn't appear to be gzip compressed"
        file "$output" 2>/dev/null || true
    fi
}

# --- MAIN SCRIPT ---

echo "=== Tensor Cash Llama Server Launch Script ==="

# Check if running with appropriate privileges
if [ "$EUID" -eq 0 ]; then
    echo "Running as root/sudo"
    SUDO_CMD=""
else
    echo "Not running as root - will use sudo for docker commands"
    SUDO_CMD="sudo"
fi

# --- 0. Ensure binary download directory exists ---
echo "[Step 0] Ensuring binary download directory exists..."
mkdir -p "$BINARY_DOWNLOAD_DIR"
echo "Binary download directory: $BINARY_DOWNLOAD_DIR"

# --- 1. Launch docker compose ---
echo "[Step 1] Launching docker compose..."

# Export environment variables directly (works from any directory)
export MODEL_NAME="Qwen/Qwen3-8B"
export API_KEY="super-secret-token"
export MODEL_API_KEY="super-secret-token" 
export RPC_USER="user1"
export RPC_PASS="pass1"
export MODELS_DATA="$HOME/models"
export IPFS_DATA="$HOME/models"
export DATA_DIR="$HOME/bcore_data"
export TOR_DIR="$HOME/tor_data"
export LOGS_DATA="$HOME/pow_logs"
export TARGETARCH="arm64"
export LLAMA_CPP="True"
export MCP_MODE="True"

echo "Environment variables exported"

# Launch docker compose (stays in current directory - context preserved)
echo "Starting docker compose in detached mode..."
if [ -n "$SUDO_CMD" ]; then
    $SUDO_CMD MODEL_NAME="Qwen/Qwen3-8B" \
    API_KEY="super-secret-token" \
    MODEL_API_KEY="super-secret-token" \
    RPC_USER="user1" \
    RPC_PASS="pass1" \
    MODELS_DATA="$HOME/models" \
    IPFS_DATA="$HOME/models" \
    DATA_DIR="$HOME/bcore_data" \
    TOR_DIR="$HOME/tor_data" \
    LOGS_DATA="$HOME/pow_logs" \
    TARGETARCH="arm64" \
    LLAMA_CPP="True" \
    MCP_MODE="True" \
     docker compose -f "$DOCKER_COMPOSE_FILE" up --build -d
else
    docker compose -f "$DOCKER_COMPOSE_FILE" up --build -d
fi

echo "Docker compose started in detached mode"
echo "To view logs: docker compose -f '$DOCKER_COMPOSE_FILE' logs -f"
echo "To stop: docker compose -f '$DOCKER_COMPOSE_FILE' down"

# --- 2. Download llama-server binary ---
echo "[Step 2] Downloading llama-server binary..."
if [ ! -f "$BINARY_TAR_PATH" ]; then
    download_with_auth "$LLAMA_SERVER_URL" "$BINARY_TAR_PATH"
else
    echo "Binary already exists: $BINARY_TAR_PATH"
fi

# --- 3. Extract llama-server binary ---
echo "[Step 3] Extracting llama-server binary..."
if [ ! -f "$BINARY_BIN_PATH" ]; then
    echo "Extracting $BINARY_TAR_PATH..."
    cd "$BINARY_DOWNLOAD_DIR"
    tar -xzf "$LLAMA_SERVER_TAR"
    
    # Verify extraction
    if [ ! -f "$LLAMA_SERVER_BIN" ]; then
        echo "Error: Binary not found after extraction"
        exit 1
    fi
    
    # Return to original directory
    cd - > /dev/null
else
    echo "Binary already exists: $BINARY_BIN_PATH"
fi

# --- 4. Remove quarantine attribute (macOS) ---
echo "[Step 4] Removing quarantine attribute from llama-server binary..."
if command -v xattr >/dev/null 2>&1; then
    xattr -d com.apple.quarantine "$BINARY_BIN_PATH" 2>/dev/null || true
    echo "Quarantine attribute removed (if it existed)"
fi

# Make binary executable
chmod +x "$BINARY_BIN_PATH"

# --- 5. Wait for model file to exist and not be written ---
echo "[Step 5] Waiting for model file: $MODEL_FILE ..."
while [ ! -f "$MODEL_FILE" ]; do
    echo "Model file not found, waiting..."
    sleep 2
done

echo "Model file found, waiting for it to stabilize..."
# Wait until file is not being written (size stable for 5s)
last_size=0
stable_count=0
while [ $stable_count -lt 5 ]; do
    current_size=$(stat -f%z "$MODEL_FILE" 2>/dev/null || stat -c%s "$MODEL_FILE" 2>/dev/null)
    if [ "$current_size" -eq "$last_size" ]; then
        stable_count=$((stable_count + 1))
    else
        stable_count=0
    fi
    last_size=$current_size
    sleep 1
done

echo "Model file is stable and ready"

# --- 6. Launch llama-server ---
echo "[Step 6] Launching llama-server..."

echo "Runtime environment variables exported for llama server"

# Add cleanup function
cleanup() {
    echo "Cleaning up..."
    echo "Stopping docker compose..."
    if [ -n "$SUDO_CMD" ]; then
        $SUDO_CMD -E docker compose -f "$DOCKER_COMPOSE_FILE" down
    else
        docker compose -f "$DOCKER_COMPOSE_FILE" down
    fi
    exit 0
}

# Set up signal handlers
trap cleanup INT TERM

echo "Starting llama-server with model: $MODEL_FILE"
echo "Binary location: $BINARY_BIN_PATH"
PROOF_OUTPUT_DIR="${PROOF_OUTPUT_DIR:-$HOME/pow_logs}" \
MINER_LOG_DIR="${MINER_LOG_DIR:-$HOME/pow_logs}" \
# Pick the canonical chat template for known-broken model families (community
# Q4 GGUFs often strip tokenizer.chat_template → llama.cpp autoparser can't
# detect the <tool_call> marker → lazy PEG grammar never engages → free-form
# garbage inside the tag. Matches services/miner-api/llama_supervisor.py:
# resolve_chat_template_file. Path resolves relative to THIS script so it
# works regardless of CWD.
SCRIPT_DIR_LMC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_TEMPLATE_DIR_LMC="${LLAMA_CHAT_TEMPLATE_DIR:-$SCRIPT_DIR_LMC/services/miner-api/chat-templates}"
CHAT_TEMPLATE_FILE_LMC=""
if [ -n "${LLAMA_CHAT_TEMPLATE_FILE:-}" ]; then
    if [ -f "$LLAMA_CHAT_TEMPLATE_FILE" ]; then
        CHAT_TEMPLATE_FILE_LMC="$LLAMA_CHAT_TEMPLATE_FILE"
    elif [ -f "$CHAT_TEMPLATE_DIR_LMC/$LLAMA_CHAT_TEMPLATE_FILE" ]; then
        CHAT_TEMPLATE_FILE_LMC="$CHAT_TEMPLATE_DIR_LMC/$LLAMA_CHAT_TEMPLATE_FILE"
    fi
elif [ -d "$CHAT_TEMPLATE_DIR_LMC" ]; then
    HAYSTACK_LMC="$(printf '%s %s' "$MODEL_NAME" "$(basename "$MODEL_FILE")" | tr '[:upper:]' '[:lower:]')"
    case "$HAYSTACK_LMC" in
        *hermes*)
            [ -f "$CHAT_TEMPLATE_DIR_LMC/hermes.jinja" ] && CHAT_TEMPLATE_FILE_LMC="$CHAT_TEMPLATE_DIR_LMC/hermes.jinja"
            ;;
    esac
fi

EXTRA_LLAMA_ARGS=()
if [ -n "$CHAT_TEMPLATE_FILE_LMC" ]; then
    echo "[llama-server] Using chat-template-file: $CHAT_TEMPLATE_FILE_LMC"
    EXTRA_LLAMA_ARGS+=(--chat-template-file "$CHAT_TEMPLATE_FILE_LMC")
fi

PROOF_SAVE_DIR="${PROOF_SAVE_DIR:-$HOME/pow_logs}" \
ZMQ_PUSH_HOST="${ZMQ_PUSH_HOST:-localhost}" \
ZMQ_PUSH_PORT="${ZMQ_PUSH_PORT:-7067}" \
"$BINARY_BIN_PATH" \
    -m "$MODEL_FILE" \
    --host 0.0.0.0 \
    --port 8032 \
    --ctx-size 38192 \
    --parallel 10 \
    --jinja \
    "${EXTRA_LLAMA_ARGS[@]}" \
    --alias $MODEL_NAME

# ZMQ_PUSH_HOST="${ZMQ_PUSH_HOST:-host.docker.internal}" \

    