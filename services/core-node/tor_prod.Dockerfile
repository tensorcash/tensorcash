# =============================================================================
# TensorCash Core Node - Production Hardened Image
# =============================================================================
#
# This is a security-hardened container image for production deployments.
#
# INCLUDES:
#   - bitcoind (headless node)
#   - bitcoin-cli
#   - cosign-bridge (Rust)
#   - api_server.py (Model API for miner-proxy to discover on-chain models)
#   - Tor (optional, for hidden service support)
#
# Does NOT include: VNC, GUI, XFCE, development tools, tests
#
# For GUI testing, use gui.Dockerfile in bcore/test-runner/
# For development with VNC, use tor.Dockerfile
#
# Security features:
#   - Multi-stage build (minimal final image)
#   - Non-root user execution
#   - Read-only root filesystem compatible
#   - Minimal attack surface
#   - Health checks
#   - Proper signal handling via tini
#
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Build Rust cosign-bridge
# -----------------------------------------------------------------------------
# Rust 1.85+ is required because transitive crates (e.g. getrandom 0.4.x)
# use edition2024 in their manifests.
FROM rust:1.87-slim-bookworm AS rust-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY services/core-node/cosign-bridge /workspace/cosign-bridge

RUN cd /workspace/cosign-bridge && \
    cargo build --release --bin cosign-bridge && \
    strip /workspace/cosign-bridge/target/release/cosign-bridge

# -----------------------------------------------------------------------------
# Stage 2: Build dependencies and Bitcoin Core
# -----------------------------------------------------------------------------
FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /build

# Install build dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    pkg-config \
    ca-certificates \
    libevent-dev \
    libssl-dev \
    libzmq3-dev \
    libsqlite3-dev \
    libgmp-dev \
    libargon2-dev \
    libboost-all-dev \
    libflint-dev \
    autoconf \
    automake \
    libtool \
    libzstd-dev \
    libsodium-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Build mkp224o (vanity .onion v3 generator)
RUN git clone --depth 1 https://github.com/cathugger/mkp224o.git /build/mkp224o && \
    cd /build/mkp224o && \
    ./autogen.sh && \
    # Explicit CFLAGS suppresses mkp224o configure's auto `-march=native`
    # (configure.ac only adds it when CFLAGS is unset). `-march=native` bakes
    # in the BUILD host's CPU instructions (AVX2 etc.); when the pod later
    # runs on a cluster node with a different/older CPU the binary dies with
    # SIGILL (Illegal instruction) and onion rotation silently never succeeds.
    # No `-march` = toolchain baseline ISA (portable across all nodes of the
    # build arch); the fast ed25519 path is hand-written asm and unaffected.
    ./configure CFLAGS="-O3 -fomit-frame-pointer -mtune=generic" && \
    make -j$(nproc) && \
    cp mkp224o /usr/local/bin/ && \
    rm -rf /build/mkp224o

# Build FlatBuffers
RUN git clone --depth 1 --branch v25.2.10 \
      https://github.com/google/flatbuffers.git /build/flatbuffers && \
    cmake -S /build/flatbuffers -B /build/flatbuffers/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DFLATBUFFERS_BUILD_TESTS=OFF && \
    cmake --build /build/flatbuffers/build -j$(nproc) && \
    cmake --install /build/flatbuffers/build && \
    rm -rf /build/flatbuffers

# Build blst (static library)
RUN git clone --depth 1 --branch v0.3.11 \
      https://github.com/supranational/blst.git /build/blst && \
    cd /build/blst && \
    ./build.sh -O3 && \
    cp libblst.a /usr/local/lib/ && \
    mkdir -p /usr/local/bindings /usr/local/include && \
    cp bindings/*.h /usr/local/bindings/ && \
    cp bindings/*.h /usr/local/include/ && \
    rm -rf /build/blst

# Install Go for kyc-prover CGO library
RUN ARCH=$(dpkg --print-architecture) && \
    wget -q https://go.dev/dl/go1.21.0.linux-${ARCH}.tar.gz && \
    tar -C /usr/local -xzf go1.21.0.linux-${ARCH}.tar.gz && \
    rm go1.21.0.linux-${ARCH}.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}"
ENV GOPATH="/go"

# Copy source trees
COPY services/core-node/bcore /build/bcore
COPY shared-utils/chiavdf /build/bcore/src/external/chiavdf
COPY shared-utils/secp256k1-zkp /build/bcore/src/external/secp256k1-zkp
COPY shared-utils/liboqs /build/bcore/src/external/liboqs
COPY shared-utils/kyc-prover /build/kyc-prover
COPY shared-utils/fb-schemas /build/fb-schemas

# Build liboqs (minimal ML-DSA only)
RUN cd /build/bcore/src/external/liboqs && \
    rm -rf build && \
    cmake -S . -B build \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF \
      -DOQS_USE_OPENSSL=OFF \
      -DOQS_BUILD_ONLY_LIB=ON \
      -DOQS_MINIMAL_BUILD="SIG_ml_dsa_44;SIG_ml_dsa_65;SIG_ml_dsa_87" \
      -DOQS_ENABLE_TEST_CONSTANT_TIME=OFF && \
    cmake --build build -j$(nproc) && \
    cmake --install build && \
    rm -rf build

# Build secp256k1-zkp (static)
RUN cd /build/bcore/src/external/secp256k1-zkp && \
    ./autogen.sh && \
    ./configure --disable-shared --enable-static \
                --enable-experimental \
                --enable-module-ecdh \
                --enable-module-extrakeys \
                --enable-module-schnorrsig \
                --enable-module-musig \
                --enable-module-ellswift \
                --enable-module-ecdsa-adaptor \
                --enable-module-recovery \
                --with-pic && \
    make -j$(nproc) && \
    make install

# Build libzkprover
RUN cd /build/kyc-prover/cgo && \
    go build -buildmode=c-shared -o libzkprover.so . && \
    cp libzkprover.so /usr/local/lib/ && \
    cp libzkprover.h /usr/local/include/

# Generate FlatBuffers headers
RUN flatc --cpp -o /build/bcore/src/rpc \
    /build/fb-schemas/proof.fbs \
    /build/fb-schemas/blockheader.fbs \
    /build/fb-schemas/validation.fbs

# Build Bitcoin Core (headless only, no tests, no GUI)
WORKDIR /build/bcore
RUN cmake -S . -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_DAEMON=ON \
      -DBUILD_CLI=ON \
      -DBUILD_GUI=OFF \
      -DBUILD_TESTS=OFF \
      -DBUILD_BENCH=OFF \
      -DWITH_ZMQ=ON \
      -DENABLE_WALLET=ON \
      -DBUILD_WALLET_TOOL=ON \
      -DREDUCE_EXPORTS=ON && \
    cmake --build build --target bitcoind bitcoin-cli bitcoin-wallet -j$(nproc) && \
    strip build/bin/bitcoind build/bin/bitcoin-cli build/bin/bitcoin-wallet

# -----------------------------------------------------------------------------
# Stage 3: Production runtime image
# -----------------------------------------------------------------------------
FROM ubuntu:22.04 AS runtime

# Security: Create non-root user early
RUN groupadd --gid 1000 tensorcash && \
    useradd --uid 1000 --gid tensorcash --shell /usr/sbin/nologin --create-home tensorcash

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Runtime libraries
    libevent-2.1-7 \
    libevent-core-2.1-7 \
    libevent-extra-2.1-7 \
    libevent-pthreads-2.1-7 \
    libssl3 \
    libzmq5 \
    libsqlite3-0 \
    libgmp10 \
    libargon2-1 \
    libboost-filesystem1.74.0 \
    libboost-locale1.74.0 \
    libboost-thread1.74.0 \
    libflint-2.8.4 \
    libzstd1 \
    libstdc++6 \
    # Python for Model API server (required for miner-proxy communication)
    python3 \
    python3-pip \
    # Process manager (proper signal handling)
    tini \
    # Supervisor for multi-process management
    supervisor \
    # Vanity onion runtime dependency
    libsodium23 \
    # Health check utilities
    netcat-openbsd \
    curl \
    ca-certificates \
    gnupg \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Tor from the OFFICIAL Tor Project apt repo — NOT Ubuntu's frozen 0.4.6.x, which
# is end-of-life: stale directory-authority data, weak stuck-bootstrap recovery,
# and no shelf life. Pin a supported floor and FAIL THE BUILD if the installed
# Tor is older, so an EOL Tor can never be silently shipped again.
ARG TOR_MIN_VERSION=0.4.8.0
RUN set -eux; \
    apt-get update; \
    wget -qO- https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc \
      | gpg --dearmor -o /usr/share/keyrings/deb.torproject.org-keyring.gpg; \
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/keyrings/deb.torproject.org-keyring.gpg] https://deb.torproject.org/torproject.org ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/tor.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends tor deb.torproject.org-keyring; \
    TOR_VER="$(tor --version | grep -oE '[0-9]+(\.[0-9]+)+' | head -1)"; \
    echo "Installed Tor ${TOR_VER} (required floor ${TOR_MIN_VERSION})"; \
    dpkg --compare-versions "${TOR_VER}" ge "${TOR_MIN_VERSION}" \
      || { echo "FATAL: Tor ${TOR_VER} is below supported floor ${TOR_MIN_VERSION}"; exit 1; }; \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies for api_server.py (minimal set)
RUN pip3 install --no-cache-dir \
    fastapi==0.109.0 \
    uvicorn[standard]==0.27.0 \
    httpx==0.26.0 \
    pydantic==2.5.3 \
    && rm -rf /root/.cache/pip

# Copy binaries from builder
COPY --from=builder /build/bcore/build/bin/bitcoind /usr/local/bin/
COPY --from=builder /build/bcore/build/bin/bitcoin-cli /usr/local/bin/
COPY --from=builder /build/bcore/build/bin/bitcoin-wallet /usr/local/bin/
COPY --from=rust-builder /workspace/cosign-bridge/target/release/cosign-bridge /usr/local/bin/
COPY --from=builder /usr/local/bin/mkp224o /usr/local/bin/
COPY services/core-node/src/vanity-onion-gen.sh /usr/local/bin/vanity-onion-gen.sh
COPY services/core-node/src/bootstrap-peers.sh /usr/local/bin/bootstrap-peers.sh
COPY services/core-node/src/start_mining.sh /usr/local/bin/start_mining.sh
COPY services/core-node/src/tor-start.sh /usr/local/bin/tor-start.sh
COPY services/core-node/src/tor-health.sh /usr/local/bin/tor-health.sh
COPY services/core-node/src/onion-claim-agent.py /usr/local/bin/onion-claim-agent.py

# Copy shared libraries
COPY --from=builder /usr/local/lib/libzkprover.so /usr/local/lib/
COPY --from=builder /usr/local/lib/liboqs.a /usr/local/lib/
RUN ldconfig

# Copy Model API server (required for miner-proxy to discover on-chain models)
COPY services/core-node/src/api_server.py /app/api_server.py
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/uint256_arithmetics.py

# Create data directories with correct ownership
RUN mkdir -p /data /var/lib/tor /var/log/tensorcash /app && \
    chown -R tensorcash:tensorcash /data /var/log/tensorcash /app && \
    chown -R debian-tor:debian-tor /var/lib/tor && \
    chmod 700 /var/lib/tor

# Configure Tor for hidden service support
RUN echo "DataDirectory /var/lib/tor" > /etc/tor/torrc.tensorcash && \
    echo "SocksPort 127.0.0.1:9050" >> /etc/tor/torrc.tensorcash && \
    echo "ControlPort 127.0.0.1:9051" >> /etc/tor/torrc.tensorcash && \
    echo "CookieAuthentication 1" >> /etc/tor/torrc.tensorcash && \
    echo "CookieAuthFileGroupReadable 1" >> /etc/tor/torrc.tensorcash && \
    # Log to stdout, NOT a file: Tor runs as debian-tor but /var/log/tensorcash is
    # owned by tensorcash (and the entrypoint re-chowns it every boot), so a file
    # log would be unwritable. Supervisor captures stdout to tor.out.log (written
    # as root) and it also surfaces via `kubectl logs` for bootstrap diagnostics.
    echo "Log notice stdout" >> /etc/tor/torrc.tensorcash && \
    chown tensorcash:tensorcash /etc/tor/torrc.tensorcash

# Create supervisord configuration for multi-process management
COPY --chmod=644 <<'SUPERVISOR_EOF' /etc/supervisor/conf.d/tensorcash.conf
[supervisord]
nodaemon=true
user=root
logfile=/var/log/tensorcash/supervisord.log
pidfile=/var/run/supervisord.pid
loglevel=info

[program:bitcoind]
command=/usr/local/bin/bitcoind -datadir=/data -conf=/data/bitcoin.conf -validationapi=real -cosignbridge=/usr/local/bin/cosign-bridge -printtoconsole
user=tensorcash
autostart=true
autorestart=true
stderr_logfile=/var/log/tensorcash/bitcoind.err.log
stdout_logfile=/var/log/tensorcash/bitcoind.out.log
priority=10
startsecs=5
stopwaitsecs=120
stopsignal=TERM

[program:api_server]
command=python3 /app/api_server.py
user=tensorcash
directory=/app
autostart=true
autorestart=true
stderr_logfile=/var/log/tensorcash/api_server.err.log
stdout_logfile=/var/log/tensorcash/api_server.out.log
priority=20
startsecs=3
# Wait for bitcoind to be ready before starting
depends_on=bitcoind

[program:tor]
command=/usr/local/bin/tor-start.sh
user=debian-tor
autostart=%(ENV_TOR_ENABLED)s
autorestart=true
stderr_logfile=/var/log/tensorcash/tor.err.log
stdout_logfile=/var/log/tensorcash/tor.out.log
priority=5

[program:tor_health]
command=/usr/local/bin/tor-health.sh
user=root
autostart=%(ENV_TOR_ENABLED)s
autorestart=true
stderr_logfile=/var/log/tensorcash/tor-health.err.log
stdout_logfile=/var/log/tensorcash/tor-health.out.log
priority=40
startsecs=0

[program:bootstrap_peers]
command=/usr/local/bin/bootstrap-peers.sh
autostart=true
autorestart=false
stderr_logfile=/var/log/tensorcash/bootstrap-peers.err.log
stdout_logfile=/var/log/tensorcash/bootstrap-peers.out.log
priority=12
startsecs=0

[program:mining]
command=/usr/local/bin/start_mining.sh
user=tensorcash
autostart=true
autorestart=true
stderr_logfile=/var/log/tensorcash/mining.err.log
stdout_logfile=/var/log/tensorcash/mining.out.log
environment=WALLET_ADDRESS="%(ENV_WALLET_ADDRESS)s"
priority=30
startsecs=10
startretries=10

[program:vanity_onion]
command=/usr/local/bin/vanity-onion-gen.sh
autostart=%(ENV_VANITY_ONION_ENABLED)s
autorestart=true
stderr_logfile=/var/log/tensorcash/vanity-onion.err.log
stdout_logfile=/var/log/tensorcash/vanity-onion.out.log
priority=15
startsecs=10

# Consumer alternative to vanity_onion: claim a pre-ground onion from an
# operator-provided onion-grinder pool (mutually exclusive with vanity_onion).
# Default off; set ONION_CLAIM_ENABLED=true + VANITY_ONION_ENABLED=false on a
# consumer node. Pool Secrets + RBAC/ServiceAccount are deployment-supplied.
[program:onion_claim]
command=python3 /usr/local/bin/onion-claim-agent.py
autostart=%(ENV_ONION_CLAIM_ENABLED)s
autorestart=true
stderr_logfile=/var/log/tensorcash/onion-claim.err.log
stdout_logfile=/var/log/tensorcash/onion-claim.out.log
priority=15
startsecs=5
SUPERVISOR_EOF

# Create entrypoint script
COPY --chmod=755 <<'ENTRYPOINT_EOF' /usr/local/bin/entrypoint.sh
#!/bin/bash
set -e

# Validate required environment for mining nodes
if [ "${VALIDATOR_HOST:-}" = "" ]; then
    echo "WARNING: VALIDATOR_HOST not set. Using default validation settings."
fi

# Set defaults
export VALIDATOR_PUSH_PORT="${VALIDATOR_PUSH_PORT:-6001}"
export VALIDATOR_PULL_PORT="${VALIDATOR_PULL_PORT:-7001}"
export API_PORT="${API_PORT:-8050}"
export TOR_ENABLED="${TOR_ENABLED:-false}"
export VANITY_ONION_ENABLED="${VANITY_ONION_ENABLED:-false}"
export ONION_CLAIM_ENABLED="${ONION_CLAIM_ENABLED:-false}"

# Chain identity — the fallback bitcoin.conf below MUST be a valid config for
# this chain (a chain-less default silently runs mainnet and is rejected by a
# tensor-test cosignbridge/validator). Mirrors miner-node.tf user_data.
export CHAIN_NAME="${CHAIN_NAME:-tensor-test}"
export P2P_PORT="${P2P_PORT:-29241}"

# API server needs these for RPC communication
export RPC_HOST="${RPC_HOST:-127.0.0.1}"
export RPC_PORT="${RPC_PORT:-8332}"
export COOKIE_FILE="${COOKIE_FILE:-/data/.cookie}"

# Create bitcoin.conf if not mounted. This is a FALLBACK only — the real conf
# is written by miner-node.tf user_data. It must still be a valid config for
# ${CHAIN_NAME}: chain= set, rpcbind/rpcport inside the [${CHAIN_NAME}] section
# (a chain-less global default silently runs mainnet), and Tor + SPV-diversity
# settings so peers/IBD work behind Tor.
if [ ! -f /data/bitcoin.conf ]; then
    echo "Creating fallback bitcoin.conf for chain ${CHAIN_NAME}..."
    cat > /data/bitcoin.conf <<EOF
chain=${CHAIN_NAME}
server=1
daemon=0
listen=1
prune=550
dbcache=64
maxmempool=50
maxconnections=16

[${CHAIN_NAME}]
port=${P2P_PORT}
listen=1
discover=1
proxy=127.0.0.1:9050
onion=127.0.0.1:9050
torcontrol=127.0.0.1:9051
listenonion=1
rpcbind=0.0.0.0
rpcallowip=172.17.0.0/16
rpcport=${RPC_PORT}
spv-onion-prefix=ten
spv-onion-tag-len=3
spv-asn-corroboration=1
EOF
    if [ -n "${BOOTSTRAP_PEER:-}" ]; then
        echo "seednode=${BOOTSTRAP_PEER}" >> /data/bitcoin.conf
        echo "addnode=${BOOTSTRAP_PEER}" >> /data/bitcoin.conf
    fi
    chown tensorcash:tensorcash /data/bitcoin.conf
fi

# Ensure log directory exists and is writable
mkdir -p /var/log/tensorcash
chown -R tensorcash:tensorcash /var/log/tensorcash

# Ensure data directory is writable
chown -R tensorcash:tensorcash /data

echo "=== TensorCash Node (Production) ==="
echo "Validator: ${VALIDATOR_HOST:-local}:${VALIDATOR_PUSH_PORT}/${VALIDATOR_PULL_PORT}"
echo "Model API: port ${API_PORT}"
echo "Tor: ${TOR_ENABLED}"
echo "===================================="

# Start supervisord to manage all processes
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
ENTRYPOINT_EOF

# Health check script
COPY --chmod=755 <<'HEALTH_EOF' /usr/local/bin/healthcheck.sh
#!/bin/bash
set -e

# Check if bitcoind is responding to RPC
bitcoin-cli -datadir=/data getblockchaininfo > /dev/null 2>&1 || exit 1

# Check if api_server is responding
curl -sf http://127.0.0.1:${API_PORT:-8050}/health > /dev/null 2>&1 || exit 1

exit 0
HEALTH_EOF

# Security: Set file permissions
RUN chmod 755 /usr/local/bin/bitcoind /usr/local/bin/bitcoin-cli \
              /usr/local/bin/bitcoin-wallet /usr/local/bin/cosign-bridge \
              /usr/local/bin/mkp224o /usr/local/bin/vanity-onion-gen.sh \
              /usr/local/bin/bootstrap-peers.sh /usr/local/bin/start_mining.sh \
              /usr/local/bin/tor-start.sh /usr/local/bin/tor-health.sh

# Note: supervisord starts as root to manage processes with different users
# (tensorcash for bitcoind/api_server, debian-tor for tor)
# Each process runs as its designated user via supervisor config
WORKDIR /data

# Expose ports
# 8333: P2P mainnet
# 18333: P2P testnet
# 8332: RPC mainnet (internal, for api_server)
# 8050: Model API (for miner-proxy)
EXPOSE 8333 18333 8050

# Volumes for persistent data
VOLUME ["/data", "/var/lib/tor"]

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

# Use tini for proper PID 1 behavior
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD []
