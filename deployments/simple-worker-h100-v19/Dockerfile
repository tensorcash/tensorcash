# Simple Worker (H100 / Hopper / vLLM v0.19) — combined backend + broker bridge.
#
# Combines the vllm_v019.Dockerfile pattern (Hopper x86_64, CUDA 12.8, vLLM 0.19
# wheel-overlay with PoW sampler) with the simple-worker supervisord+miner-proxy
# bridge, so a single container handles both inference and the WSS broker.
#
# Target VM:    Azure NCC40ads_H100_v5 (1× H100 NVL 94 GB, AMD EPYC Genoa, CC mode)
# Target model: Qwen/Qwen3.5-27B-FP8 (auto-detected via quant_method=fp8)
#
# Build (must be run on x86_64 with the v0.19 vllm worktree available):
#
#   docker buildx build --platform linux/amd64 \
#     --build-context vllm_src=/path/to/vllm-v019 \
#     -f deployments/simple-worker-h100-v19/Dockerfile \
#     -t ghcr.io/tensorcash/simple-worker:vllm-v019-h100 \
#     --push \
#     .
#
# Build context root must be the tensorcash repo root.

ARG CUDA_VERSION=12.8.0
ARG VLLM_VERSION=0.19.0
ARG PYTHON_VERSION=3.10

# ═══════════════════════════════════════════════════════════════════
# Stage 1: ChiaVDF (assembly-optimized, GMP from source)
#
# IMPORTANT: base must match the runtime stage's GLIBC (Ubuntu 22.04 = glibc
# 2.35). If you build GMP against newer glibc (e.g. python:3.10-slim →
# Debian Trixie/2.38), copying libgmp.so.10 into the runtime image makes
# every binary that dlopens it (including Triton's gcc invocation) crash with
# "version `GLIBC_2.38' not found".
# ═══════════════════════════════════════════════════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04 AS chiavdf-builder
ARG CUDA_VERSION
ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive

# Force IPv4 + retries — the build host has no IPv6 route, and apt's
# default IP-protocol preference will time out on AAAA records before
# falling back to A.
RUN printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "20";\nAcquire::http::Timeout "240";\nAcquire::http::Pipeline-Depth "0";\n' > /etc/apt/apt.conf.d/99force-ipv4

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python3-pip \
        python${PYTHON_VERSION}-venv \
        nasm yasm build-essential cmake git patch pkg-config \
        libtool autoconf automake wget m4 libboost-all-dev libflint-dev && \
    rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python && \
    ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python3

ENV GMP_VERSION=6.3.0
RUN (wget --timeout=30 --tries=2 https://ftp.gnu.org/gnu/gmp/gmp-${GMP_VERSION}.tar.xz || \
     wget --timeout=30 --tries=2 https://mirrors.kernel.org/gnu/gmp/gmp-${GMP_VERSION}.tar.xz || \
     wget --timeout=30 --tries=2 https://mirror.dogado.de/gnu/gmp/gmp-${GMP_VERSION}.tar.xz || \
     wget --timeout=30 --tries=2 https://gmplib.org/download/gmp/gmp-${GMP_VERSION}.tar.xz) && \
    tar xf gmp-${GMP_VERSION}.tar.xz && \
    cd gmp-${GMP_VERSION} && \
    ./configure --enable-fat --enable-shared --enable-static --with-pic && \
    make -j$(nproc) && make install && ldconfig && \
    cd .. && rm -rf gmp-${GMP_VERSION}*

WORKDIR /opt/chiavdf
COPY shared-utils/chiavdf /opt/chiavdf

RUN git init && \
    git config user.email "build@docker.com" && \
    git config user.name "Docker Build" && \
    git add . && \
    git commit -m "Initial commit" && \
    git tag -a v1.0.0 -m "Version 1.0.0" && \
    pip install --upgrade pip wheel setuptools setuptools_scm pybind11

# CHIAVDF_MARCH=x86-64-v3: ChiaVDF's CMakeLists defaults -march=native, which
# on the AVX-512 buildkit node produces a .so that SIGILLs (illegal instruction,
# core dumped) on hosts without AVX-512 — observed crash-looping miner-proxy on
# the RunPod 4090 host. x86-64-v3 (AVX2+BMI2+FMA) is the portable datacenter
# baseline. Same fix as services/miner-api/proxy.Dockerfile.
ENV CHIAVDF_MARCH=x86-64-v3 \
    GMP_USE_ASM=1 \
    FLINT_ENABLE_ASM=1 \
    CHIAVDF_NO_ASM="" \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    CMAKE_PREFIX_PATH=/usr/local \
    CMAKE_INCLUDE_PATH=/usr/local/include \
    CMAKE_LIBRARY_PATH=/usr/local/lib \
    BUILD_VDF_CLIENT=N

RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels -v 2>&1 | tee /build.log

# ═══════════════════════════════════════════════════════════════════
# Stage 2: C++ proof processor (proof_processor.so) + flatc
# ═══════════════════════════════════════════════════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04 AS proof-processor-builder
ARG CUDA_VERSION
ARG PYTHON_VERSION

RUN printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "20";\nAcquire::http::Timeout "240";\nAcquire::http::Pipeline-Depth "0";\n' > /etc/apt/apt.conf.d/99force-ipv4

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git wget unzip \
        libzmq3-dev pkg-config \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python3-pip \
        libssl-dev libcrypto++-dev && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp -O /usr/include/zmq.hpp

RUN python${PYTHON_VERSION} -m pip install --upgrade pip numpy pybind11

ARG FLATBUFFERS_VERSION=v23.5.26
RUN cd /tmp && \
    git clone --depth 1 --branch ${FLATBUFFERS_VERSION} https://github.com/google/flatbuffers.git && \
    cd flatbuffers && mkdir build && cd build && \
    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
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

# ═══════════════════════════════════════════════════════════════════
# Stage 3: Fetch vLLM v0.19 wheel (no-deps; we only want the .so + pure-py)
# ═══════════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS vllm-wheel-fetch
ARG VLLM_VERSION
RUN pip install --upgrade pip wheel && \
    pip wheel vllm==${VLLM_VERSION} --no-deps -w /wheels

# ═══════════════════════════════════════════════════════════════════
# Stage 4: Final unified runtime image (vLLM 0.19 + miner-proxy under supervisord)
# ═══════════════════════════════════════════════════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04
ARG CUDA_VERSION
ARG VLLM_VERSION
ARG PYTHON_VERSION

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "20";\nAcquire::http::Timeout "240";\nAcquire::http::Pipeline-Depth "0";\n' > /etc/apt/apt.conf.d/99force-ipv4

# System runtime deps + supervisor + nvcc.
#
# gcc/g++  → Triton's nvidia driver helper JIT-compiles cuda_utils.c on the
#            first launch (triton/runtime/build.py invokes $CC).
# nvcc     → flashinfer JIT-compiles GDN prefill kernels for Qwen3.5 on the
#            first inference call. Without it: 500 InternalServerError on
#            /v1/chat/completions ("/usr/local/cuda/bin/nvcc: not found").
RUN apt-get update && apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python3-pip \
        python${PYTHON_VERSION}-venv \
        gcc g++ \
        cuda-nvcc-12-8 \
        git unzip ca-certificates rsync wget curl \
        libboost-all-dev libflint-dev libzmq3-dev \
        supervisor && \
    rm -rf /var/lib/apt/lists/*

# arch-portable flatc (built from source in stage 2)
COPY --from=proof-processor-builder /usr/local/bin/flatc /usr/local/bin/flatc

# Python 3.10 default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python${PYTHON_VERSION} 1

RUN wget -q https://bootstrap.pypa.io/get-pip.py && \
    python${PYTHON_VERSION} get-pip.py && \
    rm get-pip.py

# GMP + ChiaVDF
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
# Pin setuptools <81 so pkg_resources stays available for the system
# python3-supervisor (Ubuntu 22.04 supervisord 4.2.1 imports pkg_resources).
RUN pip install --upgrade pip wheel "setuptools<81" && \
    pip install /tmp/*.whl && \
    rm -rf /tmp/*.whl

# vLLM 0.19 pinned deps
COPY services/miner-api/requirements_v19.txt /app/vllm_requirements.txt
RUN pip install --no-cache-dir -r /app/vllm_requirements.txt

# vLLM v0.19 source from named build context (decoupled from submodule HEAD)
COPY --from=vllm_src . /app/vllm

# Extract vLLM C extensions + flash_attn wrappers from upstream wheel
COPY --from=vllm-wheel-fetch /wheels/vllm-${VLLM_VERSION}-*.whl /tmp/vllm.whl
RUN SITE=/usr/local/lib/python${PYTHON_VERSION}/dist-packages && \
    unzip -q /tmp/vllm.whl -d /tmp/vllm-unpacked && \
    mkdir -p ${SITE}/vllm ${SITE}/vllm/vllm_flash_attn && \
    cp /tmp/vllm-unpacked/vllm/*.so ${SITE}/vllm/ && \
    cp -a /tmp/vllm-unpacked/vllm/vllm_flash_attn/. \
       ${SITE}/vllm/vllm_flash_attn/ && \
    rm -rf /tmp/vllm.whl /tmp/vllm-unpacked

WORKDIR /app

# Generate FlatBuffers Python files
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs && \
    flatc --python validation.fbs && \
    flatc --python blockheader.fbs && \
    mkdir -p /app/vllm/vllm/sampling/proof && \
    cp -r proof/* /app/vllm/vllm/sampling/proof/

# C++ proof processor
COPY --from=proof-processor-builder /build/tests/build/proof_processor.so \
     /usr/local/lib/python${PYTHON_VERSION}/dist-packages/

# PoW sampling helpers into vllm/sampling/
COPY shared-utils/pow-utils/common_sampler_helper.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_utils.py             /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/zmq_pow_writer.py        /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/uint256_arithmetics.py   /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/test/zmq_test_listener.py /app/vllm/vllm/sampling/

# Overlay patched pure-Python vllm onto installed compiled extensions.
# .so files stay from the PyPI wheel; .py files come from the v0.19 worktree
# carrying the PoW sampler patches (HAS_TRITON=False, sampler hook, etc.)
RUN rsync -a --exclude='*.so' \
    /app/vllm/vllm/ \
    /usr/local/lib/python${PYTHON_VERSION}/dist-packages/vllm/

# vLLM CLI entrypoint + minimal dist-info (so `vllm serve` works)
RUN printf '#!/usr/bin/python3\nimport sys\nfrom vllm.entrypoints.cli.main import main\nif __name__ == "__main__":\n    sys.exit(main())\n' \
    > /usr/local/bin/vllm && chmod +x /usr/local/bin/vllm && \
    DIST_DIR=/usr/local/lib/python${PYTHON_VERSION}/dist-packages/vllm-${VLLM_VERSION}.dist-info && \
    mkdir -p ${DIST_DIR} && \
    printf 'Metadata-Version: 2.1\nName: vllm\nVersion: %s\n' "${VLLM_VERSION}" > ${DIST_DIR}/METADATA && \
    printf 'vllm\n' > ${DIST_DIR}/top_level.txt && \
    printf '[console_scripts]\nvllm = vllm.entrypoints.cli.main:main\n' > ${DIST_DIR}/entry_points.txt && \
    touch ${DIST_DIR}/INSTALLER

# ─────────────────────────────────────────────────────────────────
# Miner-Proxy (broker bridge)
# ─────────────────────────────────────────────────────────────────
COPY services/miner-api/proxy_requirements.txt /app/proxy_requirements.txt
RUN pip install --no-cache-dir -r /app/proxy_requirements.txt

COPY services/miner-api/src /app/miner-proxy/src
COPY shared-utils/pow-utils/pow_utils.py /app/miner-proxy/src/utils/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/miner-proxy/src/utils/
COPY shared-utils/config/constants.py /app/miner-proxy/src/config/
RUN mkdir -p /app/miner-proxy/src/proof && \
    cp -r /app/proof/* /app/miner-proxy/src/proof/

# ─────────────────────────────────────────────────────────────────
# Supervisor + start scripts (reuse simple-worker scripts unchanged)
# ─────────────────────────────────────────────────────────────────
COPY deployments/simple-worker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY deployments/simple-worker/start-vllm.sh /app/start-vllm.sh
COPY deployments/simple-worker/start-proxy.sh /app/start-proxy.sh
# Optional mining vLLM instance (MINING_VLLM_ENABLED=true): a second vLLM on
# the same GPU holding the chain-registered mining model. supervisord.conf
# defines [program:vllm-mining] -> this script; it exits 0 when disabled.
COPY deployments/simple-worker/start-vllm-mining.sh /app/start-vllm-mining.sh
# Qwen3.5 enhanced chat template (allanchan339 fix). start-vllm.sh references
# /opt/chat-template/qwen3.5-enhanced.jinja via --chat-template; without this
# COPY vllm crashes at startup with ``ValueError: appears path-like, but
# doesn't exist!`` and supervisord backoff-loops the vllm process.
COPY deployments/simple-worker/chat-template/qwen3.5-enhanced.jinja /opt/chat-template/qwen3.5-enhanced.jinja
RUN chmod +x /app/start-vllm.sh /app/start-proxy.sh /app/start-vllm-mining.sh && \
    chmod 644 /opt/chat-template/qwen3.5-enhanced.jinja

# ─────────────────────────────────────────────────────────────────
# Environment defaults (H100 NVL 94 GB CC mode + Qwen3.5-27B-FP8)
# ─────────────────────────────────────────────────────────────────
ENV PYTHONPATH="/app:/app/miner-proxy/src" \
    LD_LIBRARY_PATH="/usr/local/lib"

# Persistent caches → /models volume.
# Without this, the flashinfer JIT-compiled kernels (~84s on first inference
# call) get rebuilt every time the container is rm'd. /models is the
# host-mounted docker volume so caches survive container teardown + SIG bake.
ENV XDG_CACHE_HOME=/models/.cache \
    HF_HOME=/models/.hf \
    TRITON_CACHE_DIR=/models/.cache/triton \
    HOME=/models/worker-home

# vLLM defaults — model auto-detects FP8 from config.json (quant_method=fp8)
ENV MODEL_NAME="Qwen/Qwen3.5-27B-FP8" \
    MODEL_HASH="" \
    MAX_MODEL_LEN=32768 \
    GPU_MEM_UTIL=0.85 \
    VLLM_ENABLE_POW=1 \
    API_KEY="internal-secret" \
    POW_LOG_DIR=/data/miner_logs

# Miner-proxy broker mode (standalone — no core-node sync)
ENV WORKER_MODE=broker \
    STANDALONE_MODE=true \
    TARGET_URL=http://127.0.0.1:8000 \
    HTTP_HOST=127.0.0.1 \
    HTTP_PORT=8080 \
    PROOF_CACHE_ENABLED=true \
    PROOF_COLLECTOR_PORT=7002 \
    MINING_ENABLED=true

# Worker advertise (NCC40ads_H100_v5)
ENV WORKER_CAPACITY=16 \
    COMPUTE_TYPE=nvidia-9.0 \
    GPU_MODEL=H100-NVL \
    GPU_MEMORY_GB=94 \
    WORKER_REGION=westeurope \
    MAX_CONTEXT_WINDOW=131072 \
    MAX_OUTPUT_TOKENS=32768

# Runtime dirs + non-root user
# /data hosts PoW logs + proof cache (PowLogger and ProofWriter both mkdir
# under /data/... by default in pow_utils.py — the worker user must own it).
# /models/{.cache,.hf,worker-home} pre-created so flashinfer/HF/Triton can
# write before the volume mount happens (start-up order race-safe).
RUN mkdir -p /models/.cache /models/.cache/triton /models/.hf \
        /models/worker-home /data/miner_logs /data/pow_proofs \
        /var/log/supervisor && \
    chmod 777 /models /data && \
    useradd -m -u 1000 worker && \
    chown -R worker:worker /app /models /data /var/log/supervisor

# Internal ports — broker mode means no inbound exposure needed externally
EXPOSE 8000 8080 7002

LABEL vllm.version="${VLLM_VERSION}" \
      target.gpu="H100-NVL" \
      target.compute="nvidia-9.0" \
      target.model="Qwen/Qwen3.5-27B-FP8"

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
