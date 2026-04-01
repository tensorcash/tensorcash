# Stage 1: Build Rust cosign-bridge
FROM rust:1.87 AS rust-builder
WORKDIR /workspace
COPY services/core-node/cosign-bridge /workspace/cosign-bridge
RUN cd /workspace/cosign-bridge && cargo build --release

# Stage 2: Main build
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /build

# 1) Install core build tools, libs, Tor, Qt6 for GUI, VNC, and Python
RUN apt-get update && \
    apt-get install -y \
      build-essential \
      cmake \
      git \
      pkg-config \
      curl \
      ca-certificates \
      tor \
      libevent-dev \
      libssl-dev \
      libzmq3-dev \
      libsqlite3-dev \
      libgmp-dev \
      libboost-all-dev \
      libzstd-dev \
      libflint-dev \
      autoconf \
      automake \
      libtool \
      python3 \
      python3-pip \
      supervisor \
      # Qt6 for GUI
      qt6-base-dev \
      qt6-tools-dev \
      qt6-tools-dev-tools \
      qt6-l10n-tools \
      libqt6core6 \
      libqt6gui6 \
      libqt6widgets6 \
      libqt6network6 \
      libqt6dbus6 \
      libqt6opengl6-dev \
      libgl1-mesa-dev \
      libglu1-mesa-dev \
      libqrencode-dev \
      libdb-dev \
      libdb++-dev \
      # VNC and window manager
      tigervnc-standalone-server \
      tigervnc-common \
      autocutsel \
      x11-apps \
      x11-utils \
      dbus-x11 \
      icewm \
      netcat-openbsd \
      libsodium-dev && \
    rm -rf /var/lib/apt/lists/*

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
    make -j"$(nproc)" && \
    cp mkp224o /usr/local/bin/ && \
    rm -rf /build/mkp224o

# 2) Build & install FlatBuffers v25.2.10 from source
RUN git clone --depth 1 \
      --branch v25.2.10 \
      https://github.com/google/flatbuffers.git /build/flatbuffers && \
    mkdir -p /build/flatbuffers/build && \
    cd /build/flatbuffers/build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release && \
    make -j"$(nproc)" && \
    make install && \
    ldconfig && \
    rm -rf /build/flatbuffers

# Build & install blst library with optimizations
RUN git clone --depth 1 \
      --branch v0.3.11 \
      https://github.com/supranational/blst.git /build/blst && \
    cd /build/blst && \
    ./build.sh -O3 && \
    cp libblst.a /usr/local/lib/ && \
    mkdir -p /usr/local/bindings && \
    cp bindings/*.h /usr/local/bindings/ && \
    ldconfig && \
    rm -rf /build/blst

# 3) Copy Bitcoin Core source
COPY services/core-node/bcore /build/bcore
# Bring chiavdf into a stable include location for CMake auto-detect
COPY shared-utils/chiavdf /build/bcore/src/external/chiavdf
COPY shared-utils/secp256k1-zkp /build/bcore/src/external/secp256k1-zkp

# Build liboqs for ML-DSA (FIPS 204) post-quantum verification
# Minimal build: only ML-DSA-44/65/87 for Taproot v2
COPY shared-utils/liboqs /build/bcore/src/external/liboqs
RUN cd /build/bcore/src/external/liboqs && \
    rm -rf build && mkdir build && cd build && \
    cmake -DCMAKE_INSTALL_PREFIX=/usr/local \
          -DCMAKE_BUILD_TYPE=Release \
          -DBUILD_SHARED_LIBS=ON \
          -DOQS_USE_OPENSSL=OFF \
          -DOQS_BUILD_ONLY_LIB=ON \
          -DOQS_MINIMAL_BUILD="SIG_ml_dsa_44;SIG_ml_dsa_65;SIG_ml_dsa_87" \
          -DOQS_ENABLE_TEST_CONSTANT_TIME=ON \
          .. && \
    make -j$(nproc) && \
    make install && \
    ldconfig

RUN cd /build/bcore/src/external/secp256k1-zkp && \
    ./autogen.sh && \
    ./configure --disable-shared \
                --enable-experimental \
                --enable-module-ecdh \
                --enable-module-extrakeys \
                --enable-module-schnorrsig \
                --enable-module-musig \
                --enable-module-ellswift \
                --enable-module-ecdsa-adaptor \
                --enable-module-recovery && \
    make -j"$(nproc)" && \
    make install && \
    ldconfig
WORKDIR /build/bcore

# GENERATE FBS SCHEMA FILES
COPY shared-utils/fb-schemas/proof.fbs /build/bcore/
COPY shared-utils/fb-schemas/blockheader.fbs /build/bcore/
COPY shared-utils/fb-schemas/validation.fbs /build/bcore/
RUN flatc --cpp -o src/rpc proof.fbs blockheader.fbs validation.fbs

# 4) Copy Rust cosign-bridge binary from builder
COPY --from=rust-builder /workspace/cosign-bridge/target/release/cosign-bridge /usr/local/bin/cosign-bridge
RUN chmod +x /usr/local/bin/cosign-bridge

# 5) Out-of-source build (Release) WITH GUI support
# Note: Skip cmake --install to avoid missing bitcoin-tx error; binaries accessed via PATH
RUN rm -rf build && \
    cmake -H. -Bbuild -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_GUI=ON \
      -DWITH_GUI=qt6 \
      -DWITH_ZMQ=ON \
      -DENABLE_WALLET=ON && \
    cmake --build build --target bitcoind bitcoin-cli bitcoin-wallet bitcoin-util -- -j"$(nproc)" && \
    echo "=== Core binaries built, now building bitcoin-qt ===" && \
    cmake --build build --target bitcoin-qt -- -j"$(nproc)" && \
    echo "=== Build complete. Binaries in /build/bcore/build/bin ==="

# 6) Install Python dependencies for the API server
RUN pip3 install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    httpx \
    pydantic

# 7) Copy API server script
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/uint256_arithmetics.py
COPY services/core-node/src/api_server.py /app/api_server.py
COPY services/core-node/src/tests/* /app/tests/
COPY services/core-node/src/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY services/core-node/src/start_mining.sh /build/bcore/start_mining.sh
COPY services/core-node/src/start_node.sh /build/bcore/start_node.sh
COPY services/core-node/src/vanity-onion-gen.sh /usr/local/bin/vanity-onion-gen.sh
COPY services/core-node/src/bootstrap-peers.sh /usr/local/bin/bootstrap-peers.sh
RUN chmod +x /build/bcore/start_node.sh /build/bcore/start_mining.sh /usr/local/bin/vanity-onion-gen.sh /usr/local/bin/bootstrap-peers.sh

# 8) Set up VNC server
RUN mkdir -p /root/.vnc && \
    echo "password" | vncpasswd -f > /root/.vnc/passwd && \
    chmod 600 /root/.vnc/passwd

# Create VNC startup script with IceWM (simplest WM that works in Docker)
RUN echo '#!/bin/bash' > /root/.vnc/xstartup && \
    echo '[ -f $HOME/.Xresources ] && xrdb $HOME/.Xresources' >> /root/.vnc/xstartup && \
    echo 'autocutsel -fork' >> /root/.vnc/xstartup && \
    echo 'autocutsel -selection PRIMARY -fork' >> /root/.vnc/xstartup && \
    echo '' >> /root/.vnc/xstartup && \
    echo '# Set Qt to use software rendering to avoid OpenGL issues in VNC' >> /root/.vnc/xstartup && \
    echo 'export QT_QPA_PLATFORM=xcb' >> /root/.vnc/xstartup && \
    echo 'export QT_X11_NO_MITSHM=1' >> /root/.vnc/xstartup && \
    echo 'export LIBGL_ALWAYS_SOFTWARE=1' >> /root/.vnc/xstartup && \
    echo '' >> /root/.vnc/xstartup && \
    echo '# Use IceWM - simplest window manager' >> /root/.vnc/xstartup && \
    echo 'exec icewm' >> /root/.vnc/xstartup && \
    chmod +x /root/.vnc/xstartup

# Create helper script to start VNC (runs in foreground for supervisord)
# Password is set via VNC_PASSWORD env var (default: tensorcash)
RUN echo '#!/bin/bash' > /start-vnc.sh && \
    echo 'export USER=root' >> /start-vnc.sh && \
    echo 'export HOME=/root' >> /start-vnc.sh && \
    echo 'VNC_PASS="${VNC_PASSWORD:-tensorcash}"' >> /start-vnc.sh && \
    echo 'mkdir -p /root/.vnc' >> /start-vnc.sh && \
    echo 'echo "$VNC_PASS" | vncpasswd -f > /root/.vnc/passwd' >> /start-vnc.sh && \
    echo 'chmod 600 /root/.vnc/passwd' >> /start-vnc.sh && \
    echo 'echo "Starting VNC server on port 5907 (password protected)..."' >> /start-vnc.sh && \
    echo 'vncserver -kill :7 2>/dev/null || true' >> /start-vnc.sh && \
    echo 'rm -rf /tmp/.X7-lock /tmp/.X11-unix/X7 2>/dev/null || true' >> /start-vnc.sh && \
    echo 'vncserver :7 -geometry 1280x800 -depth 24 -localhost no' >> /start-vnc.sh && \
    echo 'echo "VNC server started! Connect via: vnc://localhost:5907"' >> /start-vnc.sh && \
    echo 'echo "Password: $VNC_PASS"' >> /start-vnc.sh && \
    echo '# Keep running to satisfy supervisord' >> /start-vnc.sh && \
    echo 'sleep 2 && tail -f /root/.vnc/*:7.log 2>/dev/null || sleep infinity' >> /start-vnc.sh && \
    chmod +x /start-vnc.sh

# Create script to launch bitcoin-qt GUI
RUN echo '#!/bin/bash' > /start-gui.sh && \
    echo 'export DISPLAY=:7' >> /start-gui.sh && \
    echo 'export USER=root' >> /start-gui.sh && \
    echo 'export HOME=/root' >> /start-gui.sh && \
    echo 'export QT_QPA_PLATFORM=xcb' >> /start-gui.sh && \
    echo 'export QT_X11_NO_MITSHM=1' >> /start-gui.sh && \
    echo 'export LIBGL_ALWAYS_SOFTWARE=1' >> /start-gui.sh && \
    echo '' >> /start-gui.sh && \
    echo '# Start VNC if not already running' >> /start-gui.sh && \
    echo 'if ! xdpyinfo -display :7 >/dev/null 2>&1; then' >> /start-gui.sh && \
    echo '  echo "Starting VNC server first..."' >> /start-gui.sh && \
    echo '  /start-vnc.sh &' >> /start-gui.sh && \
    echo '  sleep 2' >> /start-gui.sh && \
    echo 'fi' >> /start-gui.sh && \
    echo '' >> /start-gui.sh && \
    echo 'echo "Starting TensorCash Qt Wallet..."' >> /start-gui.sh && \
    echo 'echo "Connect to VNC at localhost:5907"' >> /start-gui.sh && \
    echo '' >> /start-gui.sh && \
    echo '# Launch bitcoin-qt connected to tensor mainnet' >> /start-gui.sh && \
    echo 'cd /build/bcore' >> /start-gui.sh && \
    echo './build/bin/bitcoin-qt -datadir=/data -conf=/data/bitcoin.conf -validationapi=real "$@"' >> /start-gui.sh && \
    chmod +x /start-gui.sh

# 9) Expose binaries & volumes
ENV PATH="/build/bcore/build/bin:/build/bcore/build/src:${PATH}"
ENV DISPLAY=:7
ENV QT_QPA_PLATFORM=xcb
ENV QT_X11_NO_MITSHM=1
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV USER=root

# Expose ports: 8333 (P2P), 8080 (API), 5907 (VNC)
EXPOSE 8333 8080 5907

# 10) Use supervisor to manage multiple processes
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
