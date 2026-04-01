#!/bin/bash
# Start either bitcoind (headless) or bitcoin-qt (GUI) based on GUI_MODE env var

DATADIR="/data"
CONFFILE="/data/bitcoin.conf"
COSIGN_BRIDGE="/usr/local/bin/cosign-bridge"
VALIDATION_API_MODE="${VALIDATION_API_MODE:-real}"
START_MODE="http"
PASSTHROUGH_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --http)
            START_MODE="http"
            ;;
        --desktop)
            START_MODE="desktop"
            ;;
        *)
            PASSTHROUGH_ARGS+=("$arg")
            ;;
    esac
done

# Explicit transport mode switch:
# --http    => force HTTP backend (can use defaults if env vars are not set)
# --desktop => force non-HTTP (ZMQ) backend
if [ "${START_MODE}" = "http" ]; then
    VALIDATION_API_MODE="real"
    export VALIDATOR_BASE_URL="${VALIDATOR_BASE_URL:-https://localhost:9000}"
    export VALIDATOR_HTTP_TIMEOUT_MS="${VALIDATOR_HTTP_TIMEOUT_MS:-30000}"
    export VALIDATOR_API_KEY="${VALIDATOR_API_KEY:-}"
elif [ "${START_MODE}" = "desktop" ]; then
    unset VALIDATOR_HTTP_URL VALIDATOR_HTTP_URLS VALIDATOR_BASE_UR VALIDATOR_BASE_URL VALIDATOR_BASE_URLS VALIDATOR_API_KEY VALIDATOR_API_KEYS VALIDATOR_HTTP_TIMEOUT_MS
fi

# Force RPC server mode so sidecar services (api_server, start_mining.sh)
# can authenticate via cookie and call bitcoin-cli in both headless and GUI modes.
COMMON_ARGS="-datadir=${DATADIR} -conf=${CONFFILE} -server=1 -validationapi=${VALIDATION_API_MODE} -cosignbridge=${COSIGN_BRIDGE}"

# Genesis-proof generation mode should not depend on external validator readiness.
# In mock mode we can pre-approve genesis and keep quick/full checks deterministic.
if [ "${VALIDATION_API_MODE}" = "mock" ]; then
    if [ "${MOCKVAL_FORCE_EXTERNAL:-0}" = "1" ]; then
        COMMON_ARGS="${COMMON_ARGS} -mockval-force-external=1"
    fi
    if [ "${MOCKVAL_PREAPPROVE_GENESIS:-0}" = "1" ]; then
        COMMON_ARGS="${COMMON_ARGS} -mockval-preapprove-genesis=1"
    fi
    if [ -n "${MOCKVAL_DEFAULT_QUICK:-}" ]; then
        COMMON_ARGS="${COMMON_ARGS} -mockval-default-quick=${MOCKVAL_DEFAULT_QUICK}"
    fi
    if [ -n "${MOCKVAL_DEFAULT_FULL:-}" ]; then
        COMMON_ARGS="${COMMON_ARGS} -mockval-default-full=${MOCKVAL_DEFAULT_FULL}"
    fi
    if [ -n "${MOCKVAL_DEFAULT_MODEL:-}" ]; then
        COMMON_ARGS="${COMMON_ARGS} -mockval-default-model=${MOCKVAL_DEFAULT_MODEL}"
    fi
fi

if [ "${GUI_MODE:-false}" = "true" ]; then
    echo "=== Starting in GUI mode (bitcoin-qt) ==="
    echo "Connect via VNC to localhost:5907"

    # Wait for VNC to be ready
    for i in {1..30}; do
        if xdpyinfo -display :7 >/dev/null 2>&1; then
            echo "VNC display :7 is ready"
            break
        fi
        echo "Waiting for VNC display :7... ($i/30)"
        sleep 1
    done

    # Set display environment
    export DISPLAY=:7
    export QT_QPA_PLATFORM=xcb
    export QT_X11_NO_MITSHM=1
    export LIBGL_ALWAYS_SOFTWARE=1

    # Launch bitcoin-qt
    cd /build/bcore
    exec ./build/bin/bitcoin-qt ${COMMON_ARGS} "${PASSTHROUGH_ARGS[@]}"
else
    echo "=== Starting in headless mode (bitcoind) ==="
    exec bitcoind ${COMMON_ARGS} -printtoconsole "${PASSTHROUGH_ARGS[@]}"
fi
