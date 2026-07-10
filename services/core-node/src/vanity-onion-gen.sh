#!/bin/bash
# vanity-onion-gen.sh — Generate and rotate vanity .onion addresses for
# ASN corroboration diversity credit.
#
# Runs as a supervised process. On startup, checks if the current hidden
# service address is still fresh. If not (or if none exists), generates a
# new vanity onion with mkp224o, installs it, and restarts Tor + bitcoind.
#
# Env vars (with defaults matching Phase 1 C++ code):
#   VANITY_PREFIX       — vanity prefix (default: tensorc)
#   VANITY_TAG_LEN      — freshness tag length in base32 chars (default: 3)
#   FRESHNESS_WINDOW    — block window for valid tags (default: 1400)
#   ROTATION_BUFFER     — rotate when fewer than this many blocks of freshness
#                         remain (default: 200, ~1.4 days at 15s blocks)
#   HS_DIR              — hidden service directory (default: /var/lib/tor/tensorcash-service)
#   DATA_DIR            — bitcoind data directory (default: /data)
#   CONF_FILE           — bitcoin.conf path (default: /data/bitcoin.conf)
#   CHECK_INTERVAL      — seconds between freshness checks (default: 300)
#   RPC_PORT            — bitcoind RPC port (default: 29240)
#   COOKIE_FILE         — RPC cookie file (default: /data/tensor-test/.cookie)

set -euo pipefail

VANITY_PREFIX="${VANITY_PREFIX:-tensorc}"
VANITY_TAG_LEN="${VANITY_TAG_LEN:-3}"
FRESHNESS_WINDOW="${FRESHNESS_WINDOW:-1400}"
ROTATION_BUFFER="${ROTATION_BUFFER:-200}"
HS_DIR="${HS_DIR:-/var/lib/tor/tensorcash-service}"
DATA_DIR="${DATA_DIR:-/data}"
CONF_FILE="${CONF_FILE:-/data/bitcoin.conf}"
CHECK_INTERVAL="${CHECK_INTERVAL:-300}"
RPC_PORT="${RPC_PORT:-29240}"
# P2P port baked into externalip=. Testnet default is 29241; mainnet MUST
# pass P2P_PORT=39241 (the tensor chain P2P port). A hardcoded 29241 here
# advertised the wrong port for the hidden service on mainnet.
P2P_PORT="${P2P_PORT:-29241}"
COOKIE_FILE="${COOKIE_FILE:-/data/tensor-test/.cookie}"

CLI="bitcoin-cli -datadir=${DATA_DIR} -conf=${CONF_FILE}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) vanity-onion: $*"; }

# Wait for bitcoind RPC to be reachable and synced.
#
# The gate is on connected BLOCKS (>0), not headers. A headers-only tip
# (blocks=0, headers=1) is exactly what a freshly-mined block looks like
# while its body is still in async (Quick/Full) validation. Treating that
# as "synced" let an onion rotation fire and restart bitcoind mid-validation,
# which persisted the header but dropped the block body — stranding the tip
# at nTx=0/confirmations=-1 and keeping the chain at height 0 forever.
#
# Requiring blocks>0 keeps vanity fully dormant until at least one real block
# (height ≥ 1) is connected. bitcoind, mining and validation all run in their
# own supervised processes meanwhile; vanity simply does not act — and cannot
# interrupt the connection of block 1 — until the chain has actually advanced.
wait_for_node() {
    log "Waiting for bitcoind RPC..."
    while true; do
        if info=$($CLI getblockchaininfo 2>/dev/null); then
            local headers blocks
            headers=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin)['headers'])" 2>/dev/null || echo 0)
            blocks=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin)['blocks'])" 2>/dev/null || echo 0)
            if [ "$blocks" -gt 0 ] && [ "$blocks" -ge "$((headers - 10))" ]; then
                log "Node synced: blocks=$blocks headers=$headers"
                return 0
            fi
            log "Node syncing: blocks=$blocks headers=$headers"
        fi
        sleep 10
    done
}

# Quiescence guard for bitcoind restarts. Returns 0 (true) only when the node
# is fully connected to its header tip (blocks==headers) with at least one
# block beyond genesis. A rotation that restarts bitcoind while blocks<headers
# would interrupt an in-flight block connection — persisting the header but
# dropping the body. Onion rotation is never urgent enough to justify that:
# if the node is not quiescent we defer the restart, the new externalip having
# already been written to bitcoin.conf so it applies on the next clean restart.
safe_to_restart() {
    local info headers blocks
    info=$($CLI getblockchaininfo 2>/dev/null) || return 1
    headers=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin)['headers'])" 2>/dev/null || echo 0)
    blocks=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin)['blocks'])" 2>/dev/null || echo 0)
    if [ "$blocks" -gt 0 ] && [ "$blocks" -eq "$headers" ]; then
        return 0
    fi
    log "Deferring bitcoind restart: node not quiescent (blocks=$blocks headers=$headers)"
    return 1
}

# Get the tip height
get_tip_height() {
    $CLI getblockcount 2>/dev/null || echo 0
}

# Get block hash at height (raw bytes, little-endian, as hex)
# Bitcoin RPC getblockhash returns the hash in big-endian display order.
# uint256::data() stores in little-endian (byte 0 is LSB).
# We must reverse to match the C++ byte ordering.
get_block_hash_le() {
    local height=$1
    local be_hex
    be_hex=$($CLI getblockhash "$height" 2>/dev/null) || return 1
    # Reverse byte order: big-endian hex -> little-endian hex
    echo "$be_hex" | fold -w2 | tac | tr -d '\n'
}

# Base32-encode raw hex bytes and truncate to tag_len chars
# Uses the same lowercase base32 alphabet as EncodeBase32 in Bitcoin Core
hex_to_base32_tag() {
    local hex=$1
    local tag_len=$2
    python3 -c "
import base64, sys
h = '$hex'
raw_bytes_needed = ($tag_len * 5 + 7) // 8
raw = bytes.fromhex(h[:raw_bytes_needed * 2])
encoded = base64.b32encode(raw).decode().lower().rstrip('=')
print(encoded[:$tag_len])
"
}

# Derive ALL valid freshness tags from the block window.
# Returns deduplicated list of prefixes (VANITY_PREFIX + tag).
# mkp224o accepts multiple filter patterns and matches ANY of them,
# so passing the full set gives the intended ~39.6-bit effective difficulty
# (7-char prefix + 3-char tag over 1400 blocks).
get_valid_patterns() {
    local tip_height=$1
    local start=$((tip_height - FRESHNESS_WINDOW))
    [ "$start" -lt 0 ] && start=0

    # Batch RPC: fetch all block hashes in the window at once
    # Then derive tags in a single python3 invocation for speed.
    local hashes_file
    hashes_file=$(mktemp /tmp/blockhashes-XXXXXX)
    trap "rm -f '$hashes_file'" RETURN

    for h in $(seq "$start" "$tip_height"); do
        $CLI getblockhash "$h" 2>/dev/null
    done > "$hashes_file"

    python3 -c "
import base64, sys

prefix = '$VANITY_PREFIX'
tag_len = $VANITY_TAG_LEN
raw_bytes_needed = (tag_len * 5 + 7) // 8
seen = set()

for line in open('$hashes_file'):
    be_hex = line.strip()
    if not be_hex:
        continue
    # Reverse byte order: big-endian RPC hex -> little-endian (uint256::data())
    le_hex = ''.join(reversed([be_hex[i:i+2] for i in range(0, len(be_hex), 2)]))
    raw = bytes.fromhex(le_hex[:raw_bytes_needed * 2])
    tag = base64.b32encode(raw).decode().lower().rstrip('=')[:tag_len]
    pattern = prefix + tag
    if pattern not in seen:
        seen.add(pattern)
        print(pattern)
"
    rm -f "$hashes_file"
}

# Check if current onion address has a fresh tag
check_current_freshness() {
    local hostname_file="${HS_DIR}/hostname"
    [ ! -f "$hostname_file" ] && return 1

    local current_onion
    current_onion=$(tr -d '\r\n' < "$hostname_file")
    [ -z "$current_onion" ] && return 1

    # Strip .onion suffix for matching
    local addr="${current_onion%.onion}"

    # Check prefix
    if [[ "$addr" != "${VANITY_PREFIX}"* ]]; then
        log "Current onion '$current_onion' doesn't have prefix '${VANITY_PREFIX}'"
        return 1
    fi

    # Extract tag
    local tag="${addr:${#VANITY_PREFIX}:${VANITY_TAG_LEN}}"
    local tip_height
    tip_height=$(get_tip_height)
    local start=$((tip_height - FRESHNESS_WINDOW + ROTATION_BUFFER))
    [ "$start" -lt 0 ] && start=0

    # Batch-check: fetch all hashes in the safe window and scan for tag match
    local hashes_file
    hashes_file=$(mktemp /tmp/freshcheck-XXXXXX)

    for h in $(seq "$start" "$tip_height"); do
        $CLI getblockhash "$h" 2>/dev/null
    done > "$hashes_file"

    local result
    result=$(python3 -c "
import base64
tag_len = $VANITY_TAG_LEN
raw_bytes_needed = (tag_len * 5 + 7) // 8
target_tag = '$tag'
tip = $tip_height
window = $FRESHNESS_WINDOW
h = $start

for line in open('$hashes_file'):
    be_hex = line.strip()
    if not be_hex:
        h += 1
        continue
    le_hex = ''.join(reversed([be_hex[i:i+2] for i in range(0, len(be_hex), 2)]))
    raw = bytes.fromhex(le_hex[:raw_bytes_needed * 2])
    block_tag = base64.b32encode(raw).decode().lower().rstrip('=')[:tag_len]
    if block_tag == target_tag:
        remaining = window - (tip - h)
        print(f'fresh:{remaining}')
        break
    h += 1
else:
    print('stale')
" 2>/dev/null || echo "stale")

    rm -f "$hashes_file"

    if [[ "$result" == fresh:* ]]; then
        local remaining="${result#fresh:}"
        log "Current onion '$current_onion' is fresh (remaining ~${remaining} blocks)"
        return 0
    fi

    log "Current onion '$current_onion' tag '$tag' is stale or nearing expiry"
    return 1
}

# Generate a new vanity onion address
generate_vanity() {
    local tip_height
    tip_height=$(get_tip_height)

    local patterns
    patterns=$(get_valid_patterns "$tip_height")

    if [ -z "$patterns" ]; then
        log "ERROR: No valid patterns generated"
        return 1
    fi

    local pattern_count
    pattern_count=$(echo "$patterns" | wc -l | tr -d ' ')
    log "Generating vanity onion with $pattern_count distinct patterns"

    local tmpdir filter_file
    tmpdir=$(mktemp -d /tmp/vanity-gen-XXXXXX)
    filter_file=$(mktemp /tmp/vanity-filters-XXXXXX)
    echo "$patterns" > "$filter_file"

    # mkp224o -f reads one filter per line and matches ANY of them.
    # -n 1: stop after first match. -S 300: time limit in seconds.
    local rc=0
    mkp224o -f "$filter_file" -d "$tmpdir" -n 1 -S 300 || rc=$?
    rm -f "$filter_file"

    if [ "$rc" -eq 0 ]; then
        local generated_dir
        generated_dir=$(find "$tmpdir" -maxdepth 1 -mindepth 1 -type d | head -1)
        if [ -n "$generated_dir" ] && [ -f "${generated_dir}/hostname" ]; then
            local new_onion
            new_onion=$(tr -d '\r\n' < "${generated_dir}/hostname")
            log "Generated vanity onion: $new_onion"
            install_onion "$generated_dir" "$new_onion"
            rm -rf "$tmpdir"
            return 0
        fi
    fi

    rm -rf "$tmpdir"
    log "ERROR: Failed to generate vanity onion"
    return 1
}

# Install new onion keys and restart services
install_onion() {
    local src_dir=$1
    local new_onion=$2

    log "Installing new hidden service: $new_onion"

    # Back up current HS dir if it exists
    if [ -d "$HS_DIR" ]; then
        local backup="${HS_DIR}.bak.$(date +%s)"
        cp -a "$HS_DIR" "$backup"
        log "Backed up old HS to $backup"
    fi

    # Atomically replace hidden service directory
    local staging="${HS_DIR}.staging"
    rm -rf "$staging"
    mkdir -p "$staging"

    # Copy the three key files mkp224o generates
    cp "${src_dir}/hs_ed25519_public_key" "$staging/"
    cp "${src_dir}/hs_ed25519_secret_key" "$staging/"
    cp "${src_dir}/hostname" "$staging/"

    # Ownership MUST match whoever runs Tor. Prod runs Tor as `debian-tor`; if the
    # new HiddenServiceDir stays root-owned, debian-tor cannot read the key on
    # SIGHUP and the onion silently fails to load (works in dev where Tor is root,
    # breaks in prod). Derive the owner from the CURRENT HS_DIR (matches whatever
    # Tor already runs as), else fall back to debian-tor if that user exists, else
    # the current user (dev-root).
    local hs_owner=""
    if [ -d "$HS_DIR" ]; then
        hs_owner="$(stat -c '%U:%G' "$HS_DIR" 2>/dev/null || true)"
    fi
    if [ -z "$hs_owner" ]; then
        if id debian-tor >/dev/null 2>&1; then
            hs_owner="debian-tor:debian-tor"
        else
            hs_owner="$(id -un):$(id -gn)"
        fi
    fi
    chown -R "$hs_owner" "$staging" 2>/dev/null \
        || log "WARN: could not chown staging to $hs_owner; Tor may not read the new key"
    chmod 700 "$staging"
    chmod 600 "$staging/hs_ed25519_secret_key"
    log "Staged new HS dir owned by $hs_owner"

    # Atomic swap
    rm -rf "$HS_DIR"
    mv "$staging" "$HS_DIR"

    # Update bitcoin.conf externalip
    update_externalip "$new_onion"

    # Apply the new onion key by RELOADING Tor (SIGHUP), NOT restarting it.
    #
    # A full `restart tor` throws away Tor's live network state and forces a cold
    # directory bootstrap. On a node whose on-disk consensus has gone stale, that
    # cold bootstrap can wedge at 30% ("loading networkstatus consensus") for a
    # long time and isolate the node — turning a harmless onion swap into a
    # network-isolation event. SIGHUP re-reads the replaced HiddenServiceDir key
    # in place and keeps the consensus + built circuits.
    #
    # Verified on Tor 0.4.6.10 (isolated test): replacing hs_ed25519_secret_key
    # and sending SIGHUP makes Tor re-derive the new hostname with no restart.
    log "Reloading Tor (SIGHUP) to apply new onion key (no cold bootstrap)..."
    if command -v supervisorctl &>/dev/null && supervisorctl signal HUP tor &>/dev/null; then
        : # supervisor delivered the signal
    elif command -v pidof &>/dev/null && [ -n "$(pidof tor 2>/dev/null)" ]; then
        tor_pids="$(pidof tor)"
        if ! kill -HUP $tor_pids 2>/dev/null; then
            log "ERROR: kill -HUP failed for tor pid(s) [$tor_pids] — onion key NOT reloaded"
        fi
    else
        log "WARN: could not signal Tor to reload (no supervisorctl, no tor pid); new key applies on next Tor start"
    fi
    sleep 3
    # Sanity: Tor must still be running after the reload (HUP never restarts it).
    if command -v pidof &>/dev/null && [ -z "$(pidof tor 2>/dev/null)" ]; then
        log "WARN: Tor not running after SIGHUP — starting it"
        command -v supervisorctl &>/dev/null && supervisorctl start tor 2>/dev/null || true
    fi

    # Restart bitcoind to pick up new externalip — ONLY when quiescent.
    # The externalip is already persisted in bitcoin.conf; if the node is
    # mid-validation we skip the restart rather than strand the connecting
    # block. The next clean restart (or the next rotation cycle) applies it.
    if safe_to_restart; then
        log "Restarting bitcoind..."
        if command -v supervisorctl &>/dev/null; then
            supervisorctl restart node 2>/dev/null || supervisorctl restart bitcoind 2>/dev/null || true
        fi
        # Re-bootstrap peers since post-start hook won't re-run
        rebootstrap_peers
    else
        log "Skipped bitcoind restart (node not quiescent); externalip persisted in bitcoin.conf, will apply on next clean restart"
    fi

    log "Rotation complete: $new_onion"
}

# Update externalip in bitcoin.conf
update_externalip() {
    local new_onion=$1

    if grep -q "^externalip=" "$CONF_FILE" 2>/dev/null; then
        sed -i "s|^externalip=.*|externalip=${new_onion}:${P2P_PORT}|" "$CONF_FILE"
    else
        echo "externalip=${new_onion}:${P2P_PORT}" >> "$CONF_FILE"
    fi

    # Also ensure listenonion=0 (we manage the hidden service ourselves)
    if grep -q "^listenonion=" "$CONF_FILE" 2>/dev/null; then
        sed -i "s|^listenonion=.*|listenonion=0|" "$CONF_FILE"
    else
        echo "listenonion=0" >> "$CONF_FILE"
    fi

    log "Updated bitcoin.conf: externalip=${new_onion}:${P2P_PORT}, listenonion=0"
}

# Re-bootstrap peers after a bitcoind restart.
# Delegates to bootstrap-peers.sh which handles API URL parsing,
# field names, wait-for-RPC, and fallback peer correctly.
rebootstrap_peers() {
    log "Delegating to bootstrap-peers.sh..."
    /usr/local/bin/bootstrap-peers.sh || log "WARNING: bootstrap-peers.sh failed"
}

# Propagate the freshly-installed onion to the tor-seeder.
#
# This is intentionally a best-effort no-op: the seeder does NOT expose an
# onion-ingest endpoint (its API is GET-only — bootstrap-peers.sh pulls peers
# from /api/v1/peers). New onions reach the seeder organically via addrman
# gossip — we advertise `externalip=<onion>:<p2p>` with `discover=1`, peers relay
# it, and the seeder (itself a peer) harvests it. So there is nothing to push.
#
# It MUST exist and MUST NOT fail, though: main() calls it after a successful
# rotation under `set -euo pipefail`. Previously it was referenced but never
# defined, so the first grind aborted the script (command-not-found / exit 127),
# and because check_current_freshness uses a narrower window than the grinder,
# a near-edge tag could re-grind and crash-loop. Keep this defined and total.
propagate_to_seeder() {
    local hostname_file="${HS_DIR}/hostname"
    if [ -f "$hostname_file" ]; then
        log "Onion $(tr -d '\r\n' < "$hostname_file") advertised via externalip; seeder harvests it from addrman gossip (no direct push endpoint)."
    fi
    return 0
}

# Ensure bitcoin.conf has externalip set if a hidden service already exists.
# This handles the case where the init container overwrites bitcoin.conf
# from the ConfigMap on pod restart while the onion is still fresh.
ensure_config_on_startup() {
    local hostname_file="${HS_DIR}/hostname"
    if [ -f "$hostname_file" ]; then
        local current_onion
        current_onion=$(tr -d '\r\n' < "$hostname_file")
        if [ -n "$current_onion" ]; then
            local needs_restart=false

            # Check if externalip is already correct
            if ! grep -q "^externalip=${current_onion}:${P2P_PORT}" "$CONF_FILE" 2>/dev/null; then
                log "Restoring externalip=${current_onion}:${P2P_PORT} after pod restart"
                update_externalip "$current_onion"
                needs_restart=true
            fi

            if [ "$needs_restart" = true ]; then
                if safe_to_restart; then
                    log "Restarting bitcoind to apply restored config..."
                    if command -v supervisorctl &>/dev/null; then
                        supervisorctl restart node 2>/dev/null || supervisorctl restart bitcoind 2>/dev/null || true
                    fi
                    rebootstrap_peers
                else
                    log "Skipped config-restore restart (node not quiescent); restored externalip will apply on next clean restart"
                fi
            fi
        fi
    fi
}

# ============================================================================
# Main loop
# ============================================================================

main() {
    log "Starting vanity onion generator"
    log "  prefix=$VANITY_PREFIX tag_len=$VANITY_TAG_LEN"
    log "  window=$FRESHNESS_WINDOW buffer=$ROTATION_BUFFER"
    log "  check_interval=${CHECK_INTERVAL}s"

    wait_for_node

    # Restore externalip if a hidden service exists but bitcoin.conf was
    # overwritten by the init container (normal pod restart scenario)
    ensure_config_on_startup

    # Always rebootstrap peers on startup — the k8s post-start hook only
    # fires once per pod lifecycle, so if bitcoind was already running but
    # has no peers (e.g. after a vanity rotation restart), we need to re-add them.
    rebootstrap_peers

    while true; do
        if check_current_freshness; then
            log "Current onion is fresh, sleeping ${CHECK_INTERVAL}s"
        else
            log "Rotation needed"
            if generate_vanity; then
                propagate_to_seeder
            else
                log "Generation failed, will retry in ${CHECK_INTERVAL}s"
            fi
        fi
        sleep "$CHECK_INTERVAL"
    done
}

main "$@"
