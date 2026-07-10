#!/bin/bash
# Tor launcher with cold-start hardening.
#
# WHY: Tor caches the network consensus + relay microdescriptors on disk. If a
# node is down long enough for that cache to EXPIRE and then cold-starts, Tor can
# wedge at 30% ("loading networkstatus consensus") retrying relays that no longer
# exist, and stay isolated for a very long time. A long-running Tor never hits
# this because it refreshes continuously; only a cold start into a STALE cache
# does. This wrapper removes ONLY a provably-expired consensus (and the relay
# microdescs tied to it) before starting Tor, so a cold start always re-fetches a
# fresh directory instead of flailing on dead relays.
#
# It deliberately does NOT touch:
#   - state              (entry-guard selection — dropping it hurts anonymity/perf)
#   - the HiddenServiceDir(s) (onion identity keys)
#   - a consensus that is still valid (fast, normal restarts keep their warm cache)
set -u

# Resolve the torrc: prod uses /etc/tor/torrc.tensorcash; the dev/GUI image uses
# Tor's default /etc/tor/torrc. Honour an explicit TORRC override for either.
if [ -z "${TORRC:-}" ]; then
    if [ -f /etc/tor/torrc.tensorcash ]; then
        TORRC=/etc/tor/torrc.tensorcash
    else
        TORRC=/etc/tor/torrc
    fi
fi
DATADIR="$(awk '/^DataDirectory /{print $2; exit}' "$TORRC" 2>/dev/null)"
DATADIR="${DATADIR:-/var/lib/tor}"
CONS="$DATADIR/cached-microdesc-consensus"

if [ -f "$CONS" ]; then
    # "valid-until YYYY-MM-DD HH:MM:SS" (UTC) on its own line in the consensus.
    vu="$(grep -a '^valid-until ' "$CONS" 2>/dev/null | head -1 | cut -d' ' -f2-)"
    if [ -n "$vu" ]; then
        vu_epoch="$(date -u -d "$vu" +%s 2>/dev/null || echo 0)"
        now="$(date -u +%s)"
        if [ "$vu_epoch" -gt 0 ] && [ "$now" -gt "$vu_epoch" ]; then
            echo "tor-start: cached consensus EXPIRED (valid-until ${vu} UTC) — dropping stale directory cache to avoid a wedged cold bootstrap"
            rm -f "$DATADIR/cached-microdesc-consensus" \
                  "$DATADIR/cached-microdescs" \
                  "$DATADIR/cached-microdescs.new"
        else
            echo "tor-start: cached consensus still valid (valid-until ${vu} UTC) — keeping warm cache"
        fi
    fi
fi

exec /usr/bin/tor -f "$TORRC" "$@"
