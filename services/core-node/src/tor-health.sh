#!/bin/bash
# Minimal Tor/peer observability. REPORT ONLY — no auto-remediation. Emits one
# line per interval so a stuck bootstrap or peer loss is visible in `kubectl logs`
# instead of being discovered by accident (as happened once). Logs:
#   - Tor version (so an EOL Tor is obvious at a glance)
#   - bootstrap phase (PROGRESS=NN; 100 = done)
#   - circuit-established (1 = Tor can build circuits)
#   - bitcoind peer count
# tor-start.sh separately logs whether it dropped an expired dir cache at boot.
set -u
INTERVAL="${TOR_HEALTH_INTERVAL:-60}"
DATADIR="${TOR_DATADIR:-/var/lib/tor}"

tor_getinfo() {  # $1 = GETINFO key -> prints raw reply (best-effort)
    TOR_KEY="$1" TOR_DATADIR="$DATADIR" python3 - <<'PY' 2>/dev/null
import socket, binascii, os
key = os.environ["TOR_KEY"]; dd = os.environ["TOR_DATADIR"]
try:
    ck = open(os.path.join(dd, "control_auth_cookie"), "rb").read()
    s = socket.create_connection(("127.0.0.1", 9051), timeout=4)
    s.sendall(b"AUTHENTICATE " + binascii.hexlify(ck) + b"\r\n"); s.recv(64)
    s.sendall(b"GETINFO " + key.encode() + b"\r\n")
    print(s.recv(512).decode(errors="replace")); s.close()
except Exception as e:
    print("ERR " + type(e).__name__)
PY
}

tor_ver="$(tor --version 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)+' | head -1)"
echo "tor-health: monitor started (tor ${tor_ver:-unknown}, interval ${INTERVAL}s)"
while true; do
    bp="$(tor_getinfo status/bootstrap-phase | grep -oE 'PROGRESS=[0-9]+' | head -1)"
    ce="$(tor_getinfo status/circuit-established | grep -oE 'circuit-established=[01]' | head -1 | cut -d= -f2)"
    peers="$(bitcoin-cli -datadir=/data getconnectioncount 2>/dev/null || echo '?')"
    echo "tor-health: version=${tor_ver:-?} ${bp:-PROGRESS=?} circuit_established=${ce:-?} peers=${peers}"
    sleep "$INTERVAL"
done
