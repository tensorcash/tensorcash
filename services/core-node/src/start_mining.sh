#!/bin/bash
# Start mining with address rotation and monitor health.
# Runs as a long-lived supervisor process (autorestart=true).

CLI="bitcoin-cli -datadir=/data"
CHECK_INTERVAL="${MINING_CHECK_INTERVAL:-60}"   # seconds between health checks

# ── operator gate: MINING_AUTOSTART ──────────────────────────────────
# MINING_AUTOSTART=false makes this supervisor program an inert no-op:
# no wallet is created or loaded and no sovereign mining RPC is called.
# Required on chain-plane nodes running -miningbrokermode=1 (compute-
# broker orchestrated mining), where startmining/startminingwithrotation
# are refused by bcore and a local hot wallet must never exist. Also the
# right setting for viewer/simplenode deployments that share this image.
# Sleep instead of exiting so supervisord (autorestart=true) does not
# respawn-loop; changing the env requires a container restart anyway.
case "${MINING_AUTOSTART:-true}" in
    [Ff][Aa][Ll][Ss][Ee]|0|[Nn][Oo]|[Oo][Ff][Ff])
        echo "[mining] MINING_AUTOSTART=${MINING_AUTOSTART}: sovereign mining disabled — going dormant"
        exec sleep infinity
        ;;
esac

# ── wait for bitcoind RPC to be ready ────────────────────────────────
echo "[mining] Waiting for bitcoind RPC..."
for attempt in $(seq 1 60); do
    if $CLI getblockchaininfo >/dev/null 2>&1; then
        echo "[mining] bitcoind RPC ready after ${attempt}s"
        break
    fi
    sleep 1
done

if ! $CLI getblockchaininfo >/dev/null 2>&1; then
    echo "[mining] ERROR: bitcoind RPC not reachable after 60s, exiting"
    exit 1
fi

# ── compute-broker mode guard (defense in depth) ─────────────────────
# Probe with an invalid address: on a -miningbrokermode=1 node the RPC
# handler refuses BEFORE address validation (rpc/custom.cpp), so the
# refusal is deterministic; on a sovereign node this fails address
# validation without starting anything. Runs BEFORE the wallet
# load/create below so a broker-mode chain node never grows a local hot
# wallet, even if the operator forgot to set MINING_AUTOSTART=false.
mode_probe=$($CLI startmining "__miningbrokermode_probe__" 2>&1)
if echo "$mode_probe" | grep -qi "miningbrokermode"; then
    echo "[mining] Node runs -miningbrokermode=1: sovereign mining refused by bcore — going dormant"
    echo "[mining] Hint: set MINING_AUTOSTART=false on broker-mode nodes to make this explicit"
    exec sleep infinity
fi

# ── ensure wallet is loaded (required for address rotation) ──────────
WALLET_NAME="${MINING_WALLET:-miner}"
WALLET_LOAD_TIMEOUT="${WALLET_LOAD_TIMEOUT:-120}"  # seconds
loaded=$($CLI listwallets 2>&1)
if ! echo "$loaded" | grep -q "\"${WALLET_NAME}\""; then
    echo "[mining] Loading wallet '${WALLET_NAME}' (timeout=${WALLET_LOAD_TIMEOUT}s)..."
    load_result=$(timeout "$WALLET_LOAD_TIMEOUT" $CLI loadwallet "$WALLET_NAME" 2>&1)
    rc=$?
    if [ "$rc" -eq 124 ]; then
        echo "[mining] WARN: wallet load timed out after ${WALLET_LOAD_TIMEOUT}s — will use fixed address fallback"
    elif echo "$load_result" | grep -qi "error"; then
        # Wallet doesn't exist yet — create it (descriptor wallet, no passphrase)
        echo "[mining] Wallet '${WALLET_NAME}' not found, creating..."
        create_result=$($CLI createwallet "$WALLET_NAME" false false "" false true true 2>&1)
        if echo "$create_result" | grep -qi "\"name\""; then
            echo "[mining] Wallet '${WALLET_NAME}' created successfully"
        else
            echo "[mining] WARN: could not create wallet '${WALLET_NAME}': $create_result"
        fi
    else
        echo "[mining] Wallet '${WALLET_NAME}' loaded"
    fi
fi

# ── start mining ─────────────────────────────────────────────────────
start_mining() {
    # Prefer rotation (no fixed wallet needed).
    # Fall back to fixed address if WALLET_ADDRESS is set and rotation fails.
    result=$($CLI startminingwithrotation 2>&1)
    if echo "$result" | grep -qi "started"; then
        echo "[mining] $result (address rotation)"
        return 0
    fi

    # Rotation failed (e.g. no wallet loaded) — try fixed address
    if [ -n "$WALLET_ADDRESS" ]; then
        echo "[mining] Rotation failed ($result), falling back to fixed address"
        result=$($CLI startmining "$WALLET_ADDRESS" 2>&1)
        if echo "$result" | grep -qi "started"; then
            echo "[mining] $result (fixed: $WALLET_ADDRESS)"
            return 0
        fi
    fi

    echo "[mining] ERROR: could not start mining: $result"
    return 1
}

start_mining || exit 1

# ── health-check loop ────────────────────────────────────────────────
# Every CHECK_INTERVAL seconds, verify the JobSchedulerLoop is still
# pushing work. getminingmetrics increments solutions_received when
# the solution receiver gets data; but more fundamentally, if
# startminingwithrotation returns "already" the threads are alive.
echo "[mining] Health monitor started (interval=${CHECK_INTERVAL}s)"

consecutive_failures=0
while true; do
    sleep "$CHECK_INTERVAL"

    # Quick liveness: try to start mining — "already" means it's running
    probe=$($CLI startminingwithrotation 2>&1 || $CLI startmining "${WALLET_ADDRESS:-dummy}" 2>&1)

    if echo "$probe" | grep -qi "already"; then
        consecutive_failures=0
        # Periodic metrics log
        metrics=$($CLI getminingmetrics 2>&1) || true
        echo "[mining] OK — $metrics"
        continue
    fi

    # If it returned "was started", the threads had died and we just restarted them
    if echo "$probe" | grep -qi "started"; then
        consecutive_failures=0
        echo "[mining] RECOVERED — mining threads had stopped, restarted: $probe"
        continue
    fi

    consecutive_failures=$((consecutive_failures + 1))
    echo "[mining] WARN — unexpected probe response ($consecutive_failures): $probe"

    if [ "$consecutive_failures" -ge 5 ]; then
        echo "[mining] ERROR — 5 consecutive health failures, exiting for supervisor restart"
        exit 1
    fi
done
