# Simple Worker: Combined vLLM + Miner-Proxy in single container
# Broker mode only - connects outbound via WSS, no inbound HTTPS needed
#
# Build: docker build -t tensorcash/simple-worker:latest .
# Run:   docker run --gpus all -e BROKER_WS_URL=wss://... -e PROVIDER_JWT_TOKEN=... tensorcash/simple-worker

ARG CUDA_VERSION=12.8.0
ARG VLLM_VERSION=0.10.0

# =============================================================================
# Stage 1: Build ChiaVDF with assembly optimizations
# =============================================================================
FROM python:3.10-slim AS chiavdf-builder

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

RUN git init && \
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
# Stage 2: Build C++ proof processor
# =============================================================================
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04 AS proof-processor-builder
ARG CUDA_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git wget unzip \
        libzmq3-dev pkg-config \
        python3.10 python3.10-dev python3-pip \
        libssl-dev libcrypto++-dev && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp -O /usr/include/zmq.hpp

RUN python3.10 -m pip install --upgrade pip numpy pybind11

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
# Stage 3: Fetch vLLM wheel
# =============================================================================
FROM python:3.10-slim AS vllm-wheel-fetch
ARG VLLM_VERSION
RUN pip install --upgrade pip wheel && \
    pip wheel vllm==${VLLM_VERSION} --no-deps -w /wheels

# =============================================================================
# Stage 4: Final unified runtime image
# =============================================================================
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04
ARG CUDA_VERSION
ARG VLLM_VERSION

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install system runtime dependencies + supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-dev python3-pip \
        git unzip ca-certificates rsync wget curl \
        libboost-all-dev libflint-dev libzmq3-dev \
        supervisor && \
    rm -rf /var/lib/apt/lists/*

# Install FlatBuffers binary
RUN wget https://github.com/google/flatbuffers/releases/download/v23.5.26/Linux.flatc.binary.g%2B%2B-10.zip && \
    unzip Linux.flatc.binary.g++-10.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/flatc && \
    rm Linux.flatc.binary.g++-10.zip

# Set Python 3.10 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1

RUN wget https://bootstrap.pypa.io/get-pip.py && python3.10 get-pip.py && rm get-pip.py
# setuptools 81 dropped the legacy ``pkg_resources`` shim. The apt-installed
# supervisord script imports ``pkg_resources`` from setuptools, so leaving pip
# free to upgrade past 81 puts the runtime in a state where supervisord
# crash-loops with ``ModuleNotFoundError: No module named 'pkg_resources'``
# at container start. Pin to <81 to retain the shim.
RUN pip install --no-cache-dir 'setuptools<81'

# Copy GMP libraries from builder
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig

# Install ChiaVDF
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
RUN pip install --upgrade pip wheel 'setuptools<81' && \
    pip install /tmp/*.whl && rm -rf /tmp/*.whl

# -----------------------------------------------------------------------------
# vLLM Setup
# -----------------------------------------------------------------------------
COPY services/miner-api/requirements_v10.txt /app/vllm_requirements.txt
RUN pip install --no-cache-dir -r /app/vllm_requirements.txt

COPY services/miner-api/vllm-v010 /app/vllm

# Generate FlatBuffers Python files
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs && \
    flatc --python validation.fbs && \
    flatc --python blockheader.fbs

# Copy generated flatbuffer files to vllm/sampling/proof
RUN mkdir -p /app/vllm/vllm/sampling/proof && \
    cp -r proof/* /app/vllm/vllm/sampling/proof/

# Copy proof processor module
COPY --from=proof-processor-builder /build/tests/build/proof_processor.so /usr/local/lib/python3.10/dist-packages/

# Copy pow_utils to vllm/sampling
COPY shared-utils/pow-utils/common_sampler_helper.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_utils.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/zmq_pow_writer.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/test/zmq_test_listener.py /app/vllm/vllm/sampling/

# Extract vLLM C extension from wheel
COPY --from=vllm-wheel-fetch /wheels/vllm-${VLLM_VERSION}-*.whl /tmp/vllm.whl
RUN unzip -q /tmp/vllm.whl -d /tmp/vllm-unpacked && \
    cp /tmp/vllm-unpacked/vllm/_C*.so /usr/local/lib/python3.10/dist-packages/vllm/ && \
    rm -rf /tmp/vllm.*

# Overlay pure-Python bits
RUN rsync -a --exclude='*.so' /app/vllm/vllm/ /usr/local/lib/python3.10/dist-packages/vllm/

# -----------------------------------------------------------------------------
# Miner-Proxy Setup
# -----------------------------------------------------------------------------
COPY services/miner-api/proxy_requirements.txt /app/proxy_requirements.txt
RUN pip install --no-cache-dir -r /app/proxy_requirements.txt

# Copy miner-proxy source
COPY services/miner-api/src /app/miner-proxy/src
COPY shared-utils/pow-utils/pow_utils.py /app/miner-proxy/src/utils/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/miner-proxy/src/utils/
COPY shared-utils/config/constants.py /app/miner-proxy/src/config/

# Copy FlatBuffer generated files for miner-proxy
RUN mkdir -p /app/miner-proxy/src/proof && \
    cp -r /app/proof/* /app/miner-proxy/src/proof/

# -----------------------------------------------------------------------------
# Supervisord Configuration
# -----------------------------------------------------------------------------
COPY deployments/simple-worker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY deployments/simple-worker/start-vllm.sh /app/start-vllm.sh
COPY deployments/simple-worker/start-proxy.sh /app/start-proxy.sh
COPY deployments/simple-worker/start-vllm-mining.sh /app/start-vllm-mining.sh
# Qwen3.5 enhanced chat template (allanchan339/vLLM-Qwen3-3.5-3.6-chat-template-fix).
# Stock qwen3.5_official.jinja fires <|im_end|> prematurely on XML tool emission;
# this template fixes interleaved-thinking + tool_call placement so qwen3_xml
# tool-call-parser can actually see the call. Without this the model just
# stops after </think>. See start-vllm.sh for the --chat-template flag.
COPY deployments/simple-worker/chat-template/qwen3.5-enhanced.jinja /opt/chat-template/qwen3.5-enhanced.jinja
RUN chmod +x /app/start-vllm.sh /app/start-proxy.sh /app/start-vllm-mining.sh

# -----------------------------------------------------------------------------
# Environment & Ports
# -----------------------------------------------------------------------------
ENV PYTHONPATH="/app:/app/miner-proxy/src:${PYTHONPATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# vLLM defaults
ENV MODEL_NAME="Qwen/Qwen3-8B"
ENV MODEL_HASH=""
ENV MAX_MODEL_LEN=8192
ENV GPU_MEM_UTIL=0.9
ENV VLLM_ENABLE_POW=1
ENV API_KEY="internal-secret"

# Optional second vLLM + miner-proxy pair holding the chain-registered
# mining model alongside the primary inference model (dual-model GPU).
# Disabled by default; see start-vllm-mining.sh / start-proxy-mining.sh.
ENV MINING_VLLM_ENABLED=false

# Miner-proxy broker mode defaults (standalone - no core-node model sync)
ENV WORKER_MODE=broker
ENV STANDALONE_MODE=true
ENV TARGET_URL=http://127.0.0.1:8000
ENV HTTP_HOST=127.0.0.1
ENV HTTP_PORT=8080
ENV PROOF_CACHE_ENABLED=true
ENV PROOF_COLLECTOR_PORT=7002
ENV MINING_ENABLED=true

# Broker connection (must be provided at runtime)
# ENV BROKER_WS_URL=wss://broker.example.com/v1/ws
# ENV PROVIDER_JWT_TOKEN=eyJ...

# Worker capabilities (customize at runtime)
ENV WORKER_CAPACITY=4
ENV COMPUTE_TYPE=nvidia-8.9
ENV GPU_MODEL=RTX-4090
ENV GPU_MEMORY_GB=24
ENV WORKER_REGION=us-west-2
# Context/output limits (required by broker for scheduling)
ENV MAX_CONTEXT_WINDOW=128000
ENV MAX_OUTPUT_TOKENS=8192

# Create directories and user
RUN mkdir -p /models /var/log/supervisor && chmod 777 /models && \
    useradd -m -u 1000 worker && chown -R worker:worker /app /models /var/log/supervisor

# Internal ports only - no external exposure needed in broker mode
# vLLM: 8000 (internal), Miner-proxy: 8080 (internal), Proof collector: 7002 (internal)
EXPOSE 8000 8080 7002

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
