# Simple Worker CPU: llama.cpp + Miner-Proxy in single container
# For testing the full pipeline (Key Vault, broker registration, attestation)
# on CPU-only VMs like Azure DC2as_v5 (no GPU).
#
# Build (from tensorcash/ root):
#   docker buildx build --platform linux/amd64 -f deployments/simple-worker-cpu/Dockerfile -t ghcr.io/tensorcash/simple-worker-cpu:latest .
#
# Run:
#   docker run -e BROKER_WS_URL=wss://... -e PROVIDER_JWT_TOKEN=... ghcr.io/tensorcash/simple-worker-cpu:latest

# =============================================================================
# Stage 1: Build ChiaVDF (identical to simple-worker/Dockerfile)
# =============================================================================
FROM python:3.10-slim-bookworm AS chiavdf-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nasm yasm build-essential cmake git patch pkg-config \
        libtool autoconf automake wget m4 libboost-all-dev libflint-dev && \
    rm -rf /var/lib/apt/lists/*

ENV GMP_VERSION=6.3.0
RUN (wget --timeout=30 --tries=2 https://ftp.gnu.org/gnu/gmp/gmp-${GMP_VERSION}.tar.xz || \
     wget --timeout=30 --tries=2 https://mirrors.kernel.org/gnu/gmp/gmp-${GMP_VERSION}.tar.xz || \
     wget --timeout=30 --tries=2 https://mirror.dogado.de/gnu/gmp/gmp-${GMP_VERSION}.tar.xz || \
     wget --timeout=30 --tries=2 https://gmplib.org/download/gmp/gmp-${GMP_VERSION}.tar.xz) && \
    tar xf gmp-${GMP_VERSION}.tar.xz && \
    cd gmp-${GMP_VERSION} && \
    ./configure --enable-assembly --enable-shared --enable-static --with-pic && \
    make -j$(nproc) && make install && ldconfig && \
    cd .. && rm -rf gmp-${GMP_VERSION}*

WORKDIR /opt/chiavdf
COPY shared-utils/chiavdf /opt/chiavdf

# Reset git state + disable LTO (breaks under QEMU cross-compile)
RUN rm -rf .git && \
    sed -i 's/INTERPROCEDURAL_OPTIMIZATION TRUE/INTERPROCEDURAL_OPTIMIZATION FALSE/' src/CMakeLists.txt && \
    git init && \
    git config user.email "build@docker.com" && \
    git config user.name "Docker Build" && \
    git add . && git commit -m "Initial commit" && \
    git tag -a v1.0.0 -m "Version 1.0.0" && \
    pip install --upgrade pip wheel setuptools setuptools_scm pybind11

ENV GMP_USE_ASM=1 \
    FLINT_ENABLE_ASM=1 \
    CHIAVDF_NO_ASM="" \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    CMAKE_PREFIX_PATH=/usr/local \
    CMAKE_INCLUDE_PATH=/usr/local/include \
    CMAKE_LIBRARY_PATH=/usr/local/lib \
    BUILD_VDF_CLIENT=N

RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels

# =============================================================================
# Stage 2: Build llama.cpp (CPU-only) with PoW integration
# =============================================================================
FROM python:3.10-slim-bookworm AS llama-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake curl git libomp-dev libcurl4-openssl-dev \
        libzmq3-dev libzmq5 cppzmq-dev pkg-config \
        unzip ca-certificates wget \
        libssl-dev libargon2-dev && \
    rm -rf /var/lib/apt/lists/*

# Build FlatBuffers from source
ARG FLATBUFFERS_VERSION=v23.5.26
RUN cd /tmp && \
    git clone --depth 1 --branch ${FLATBUFFERS_VERSION} https://github.com/google/flatbuffers.git && \
    cd flatbuffers && mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release \
        -DFLATBUFFERS_BUILD_TESTS=OFF \
        -DFLATBUFFERS_BUILD_FLATLIB=ON \
        -DFLATBUFFERS_BUILD_FLATC=ON \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    make -j$(nproc) && make install && ldconfig && \
    cd / && rm -rf /tmp/flatbuffers

WORKDIR /app

# Copy llama.cpp source + PoW extensions
COPY services/miner-api/llama.cpp /app/llama.cpp
COPY shared-utils/pow-utils/*.h /app/llama.cpp/tools/server/
COPY shared-utils/pow-utils/*.cpp /app/llama.cpp/tools/server/
COPY shared-utils/fb-schemas/proof.fbs /app/llama.cpp/tools/server/
COPY shared-utils/fb-schemas/blockheader.fbs /app/llama.cpp/tools/server/
COPY shared-utils/fb-schemas/validation.fbs /app/llama.cpp/tools/server/

# Build llama.cpp for CPU
WORKDIR /app/llama.cpp
RUN rm -rf build && mkdir build && cd build && \
    cmake .. \
        -DLLAMA_SERVER=ON \
        -DGGML_NATIVE=OFF \
        -DFlatBuffers_DIR=/usr/local/lib/cmake/flatbuffers \
        -DCMAKE_BUILD_TYPE=Release && \
    make -j$(nproc)

# =============================================================================
# Stage 3: Build C++ proof processor (CPU)
# =============================================================================
FROM python:3.10-slim-bookworm AS proof-processor-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git wget unzip \
        libzmq3-dev pkg-config \
        python3-dev \
        libssl-dev libcrypto++-dev libargon2-dev && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp -O /usr/include/zmq.hpp
RUN pip install --upgrade pip numpy pybind11

ARG FLATBUFFERS_VERSION=v23.5.26
RUN cd /tmp && \
    git clone --depth 1 --branch ${FLATBUFFERS_VERSION} https://github.com/google/flatbuffers.git && \
    cd flatbuffers && mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release \
        -DFLATBUFFERS_BUILD_TESTS=OFF \
        -DFLATBUFFERS_BUILD_FLATLIB=ON \
        -DFLATBUFFERS_BUILD_FLATC=ON \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    make -j$(nproc) && make install && ldconfig && \
    cd / && rm -rf /tmp/flatbuffers

WORKDIR /build
COPY shared-utils/pow-utils/ ./
COPY shared-utils/fb-schemas/ ./fb-schemas/
RUN FB_SCHEMAS_DIR=/build/fb-schemas bash -c '. tests/build_proofprocessor_simple.sh'

# =============================================================================
# Stage 4: Final runtime image (CPU-only, no CUDA)
# =============================================================================
FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# System runtime deps + supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
        git unzip ca-certificates rsync wget curl \
        libboost-all-dev libflint-dev libzmq3-dev libargon2-1 \
        libomp5 libcurl4 libgomp1 \
        supervisor && \
    rm -rf /var/lib/apt/lists/*

# Copy FlatBuffers binary + libs from builder (arch-independent)
COPY --from=llama-builder /usr/local/bin/flatc /usr/local/bin/flatc
COPY --from=llama-builder /usr/local/lib/libflatbuffers* /usr/local/lib/
RUN ldconfig

# Copy GMP libs from ChiaVDF builder
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig

# Install ChiaVDF
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
RUN pip install --upgrade pip setuptools wheel && \
    pip install /tmp/*.whl && rm -rf /tmp/*.whl

# Copy llama-server binary + shared libraries from builder
COPY --from=llama-builder /app/llama.cpp/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=llama-builder /app/llama.cpp/build/bin/llama-cli /usr/local/bin/llama-cli
COPY --from=llama-builder /app/llama.cpp/build/bin/libllama.so /usr/local/lib/
COPY --from=llama-builder /app/llama.cpp/build/bin/libggml.so /usr/local/lib/
COPY --from=llama-builder /app/llama.cpp/build/bin/libggml-base.so /usr/local/lib/
COPY --from=llama-builder /app/llama.cpp/build/bin/libggml-cpu.so /usr/local/lib/
COPY --from=llama-builder /app/llama.cpp/build/bin/libmtmd.so /usr/local/lib/
RUN ldconfig

# HuggingFace model download + GGUF conversion deps
COPY services/miner-api/llama.cpp/requirements /tmp/llama-reqs/
RUN pip install huggingface_hub transformers && \
    pip install -r /tmp/llama-reqs/requirements-convert_hf_to_gguf.txt && \
    rm -rf /tmp/llama-reqs
COPY services/miner-api/llama.cpp/convert_hf_to_gguf.py /app/convert_hf_to_gguf.py
COPY services/miner-api/llama.cpp/gguf-py /app/gguf-py
RUN pip install /app/gguf-py

# Copy model preparation script
COPY services/miner-api/prepare_model.py /app/prepare_model.py

# Generate FlatBuffers Python files
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs && \
    flatc --python validation.fbs && \
    flatc --python blockheader.fbs

# -----------------------------------------------------------------------------
# Miner-Proxy Setup
# -----------------------------------------------------------------------------
COPY services/miner-api/proxy_requirements.txt /app/proxy_requirements.txt
RUN pip install --no-cache-dir -r /app/proxy_requirements.txt

COPY services/miner-api/src /app/miner-proxy/src
COPY shared-utils/pow-utils/pow_utils.py /app/miner-proxy/src/utils/
COPY shared-utils/pow-utils/pow_v3.py /app/miner-proxy/src/utils/
COPY shared-utils/pow-utils/bcred_table_r1024.py /app/miner-proxy/src/utils/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/miner-proxy/src/utils/
COPY shared-utils/config/constants.py /app/miner-proxy/src/config/

# Copy FlatBuffer generated files for miner-proxy
RUN mkdir -p /app/miner-proxy/src/proof && \
    cp -r /app/proof/* /app/miner-proxy/src/proof/

# Copy proof processor (may land at python3.11 path on bookworm)
COPY --from=proof-processor-builder /build/tests/build/proof_processor*.so /usr/local/lib/python3.10/dist-packages/

# -----------------------------------------------------------------------------
# Supervisord + startup scripts
# -----------------------------------------------------------------------------
COPY deployments/simple-worker-cpu/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY deployments/simple-worker-cpu/start-llama.sh /app/start-llama.sh
COPY deployments/simple-worker/start-proxy.sh /app/start-proxy.sh
# Canonical chat templates (see services/miner-api/llama_supervisor.py for the
# autoparser/grammar rationale).
COPY services/miner-api/chat-templates /app/chat-templates
RUN chmod +x /app/start-llama.sh /app/start-proxy.sh

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------
ENV PYTHONPATH="/app:/app/miner-proxy/src:${PYTHONPATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# Model defaults (tiny model for CPU testing)
ENV MODEL_NAME="Qwen/Qwen2.5-0.5B-Instruct"
ENV MODEL_COMMIT=""
ENV MAX_MODEL_LEN=2048
ENV API_KEY="internal-secret"
ENV LLAMA_CHAT_TEMPLATE_DIR=/app/chat-templates

# Miner-proxy broker mode
ENV WORKER_MODE=broker
ENV STANDALONE_MODE=true
ENV TARGET_URL=http://127.0.0.1:8000
ENV HTTP_HOST=127.0.0.1
ENV HTTP_PORT=8080
ENV LLAMA_CPP=True
ENV USE_VLLM_XARGS=false
ENV PROOF_CACHE_ENABLED=true
ENV PROOF_COLLECTOR_PORT=7002
ENV MINING_ENABLED=true

# Worker capabilities (CPU)
ENV WORKER_CAPACITY=1
ENV COMPUTE_TYPE=cpu
ENV GPU_MODEL=none
ENV GPU_MEMORY_GB=0
ENV WORKER_REGION=westeurope
ENV MAX_CONTEXT_WINDOW=2048
ENV MAX_OUTPUT_TOKENS=512

RUN mkdir -p /models /var/log/supervisor && chmod 777 /models && \
    useradd -m -u 1000 worker && chown -R worker:worker /app /models /var/log/supervisor

EXPOSE 8000 8080 7002

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=180s \
    CMD curl -f http://127.0.0.1:8080/health || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
