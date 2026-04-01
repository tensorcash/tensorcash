#!/bin/bash
# bootstrap-peers.sh — Fetch peers from the seeder API and add them via bitcoin-cli.
# Runs once after bitcoind is reachable. Complements the hardcoded vSeeds in
# chainparams.cpp with dynamic peer discovery over HTTPS.
#
# Env vars:
#   SEEDER_API_URL  — seeder API endpoint (default: https://seeds.tensorcash.org/api/v1/peers?type=onion)
#   DATA_DIR        — bitcoind data directory (default: /data)
#   CONF_FILE       — bitcoin.conf path (default: /data/bitcoin.conf)
#   BOOTSTRAP_PEER  — fallback peer if API returns nothing (default: empty)

set -euo pipefail

SEEDER_API_URL="${SEEDER_API_URL:-https://seeds.tensorcash.org/api/v1/peers?type=onion}"
DATA_DIR="${DATA_DIR:-/data}"
CONF_FILE="${CONF_FILE:-/data/bitcoin.conf}"
BOOTSTRAP_PEER="${BOOTSTRAP_PEER:-}"
ISOLATED_NODE="${ISOLATED_NODE:-false}"
CLI="bitcoin-cli -datadir=${DATA_DIR} -conf=${CONF_FILE}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) bootstrap-peers: $*"; }

case "${ISOLATED_NODE,,}" in
    1|true|yes|y|on)
        log "ISOLATED_NODE is enabled; skipping peer bootstrap"
        exit 0
        ;;
esac

# Wait for bitcoind RPC
log "Waiting for bitcoind RPC..."
attempts=0
while ! $CLI getblockchaininfo &>/dev/null; do
    sleep 5
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 60 ]; then
        log "bitcoind not responding after 5 minutes, giving up"
        exit 1
    fi
done
log "bitcoind RPC is up"

# Fetch peers from seeder API
peers_json=$(curl -fsS --connect-timeout 10 --max-time 15 "$SEEDER_API_URL" 2>/dev/null || true)

if [ -n "$peers_json" ]; then
    peers=$(python3 -c "
import json, sys
try:
    data = json.loads('''$peers_json''')
    for p in data.get('peers', []):
        addr = p.get('address', '')
        port = p.get('port', '')
        if addr and port:
            print(f'{addr}:{port}')
except Exception:
    pass
" 2>/dev/null || true)

    if [ -n "$peers" ]; then
        count=0
        echo "$peers" | while read -r addr; do
            $CLI addnode "$addr" onetry 2>/dev/null || true
            count=$((count + 1))
        done
        log "Added $(echo "$peers" | wc -l | tr -d ' ') peers from seeder API"
    else
        log "No peers parsed from API response"
    fi
else
    log "Seeder API unreachable or returned empty"
fi

# Always try bootstrap peer as fallback
if [ -n "$BOOTSTRAP_PEER" ]; then
    $CLI addnode "$BOOTSTRAP_PEER" onetry 2>/dev/null || true
    log "Added bootstrap peer: $BOOTSTRAP_PEER"
fi

log "Done"
