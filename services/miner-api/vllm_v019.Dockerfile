# Multi-stage build for vLLM v0.19 + PoW sampler (canonical contract).
# Ported from vllm_v016.Dockerfile.
#
# The vLLM v0.19 source is supplied via a NAMED build context (vllm_src) so
# the submodule HEAD does not need to be moved off feature/pow-on-v0.16.
# Pass the v0.19 worktree path with --build-context:
#
#   docker buildx build --builder multiarch --platform linux/arm64 \
#     --build-context vllm_src=services/miner-api/vllm-v019 \
#     -f services/miner-api/vllm_v019.Dockerfile \
#     -t ghcr.io/tensorcash/vllm-backend:blackwell-arm64-pow-v19-src \
#     --push \
#     .
#
# The main build context must be the tensorcash repo root (it provides
# shared-utils/, services/miner-api/requirements_v19.txt, start.sh, etc).
#
# This image force-disables Triton (HAS_TRITON=False in vllm/triton_utils/importing.py)
# to keep the sampler on the deterministic pytorch sort path required for PoW
# proof reproducibility. Dense models without LoRA / speculative decoding only.

ARG CUDA_VERSION=12.8.0
ARG VLLM_VERSION=0.19.0
ARG PYTHON_VERSION=3.10

# ═══════════════════════════════════════════════════════════════════
# Stage 1: Build ChiaVDF with assembly optimizations
# ═══════════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS chiavdf-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nasm yasm build-essential cmake git patch pkg-config \
        libtool autoconf automake wget m4 libboost-all-dev libflint-dev && \
    rm -rf /var/lib/apt/lists/*

# Build GMP from source with asm + C++ support
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

WORKDIR /opt
COPY shared-utils/chiavdf /opt/chiavdf

WORKDIR /opt/chiavdf
RUN git init && \
    git config user.email "build@docker.com" && \
    git config user.name "Docker Build" && \
    git add . && \
    git commit -m "Initial commit" && \
    git tag -a v1.0.0 -m "Version 1.0.0"
RUN pip install --upgrade pip wheel setuptools setuptools_scm pybind11

ENV GMP_USE_ASM=1
ENV FLINT_ENABLE_ASM=1
ENV CHIAVDF_NO_ASM=""
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
ENV CMAKE_PREFIX_PATH=/usr/local
ENV CMAKE_INCLUDE_PATH=/usr/local/include
ENV CMAKE_LIBRARY_PATH=/usr/local/lib
ENV BUILD_VDF_CLIENT=N

RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels -v 2>&1 | tee /build.log

# ═══════════════════════════════════════════════════════════════════
# Stage 2: Build C++ proof processor (proof_processor.so)
# ═══════════════════════════════════════════════════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04 AS proof-processor-builder
ARG CUDA_VERSION
ARG PYTHON_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git wget unzip \
        libzmq3-dev pkg-config \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python3-pip \
        libssl-dev libcrypto++-dev libargon2-dev && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp \
    -O /usr/include/zmq.hpp

RUN python${PYTHON_VERSION} -m pip install --upgrade pip numpy pybind11

ARG FLATBUFFERS_VERSION=v23.5.26
RUN cd /tmp && \
    git clone --depth 1 --branch ${FLATBUFFERS_VERSION} \
        https://github.com/google/flatbuffers.git && \
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
# Stage 3: Fetch vLLM v0.19 wheel (no-deps, just the .so + pure-py)
# ═══════════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS vllm-wheel-fetch
ARG VLLM_VERSION
RUN pip install --upgrade pip wheel && \
    pip wheel vllm==${VLLM_VERSION} --no-deps -w /wheels

# ═══════════════════════════════════════════════════════════════════
# Stage 4: Final runtime image
# ═══════════════════════════════════════════════════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04
ARG CUDA_VERSION
ARG VLLM_VERSION
ARG PYTHON_VERSION

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# System runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python3-pip \
        python${PYTHON_VERSION}-venv \
        git unzip ca-certificates rsync wget \
        libboost-all-dev libflint-dev libzmq3-dev libargon2-1 && \
    rm -rf /var/lib/apt/lists/*

# FlatBuffers binary (arch-portable: reuse the flatc that proof-processor-builder
# already built from source, so this Dockerfile works on arm64 / aarch64 as well
# as amd64. The upstream Linux.flatc.binary.g++-10.zip is amd64-only.)
COPY --from=proof-processor-builder /usr/local/bin/flatc /usr/local/bin/flatc

# Set Python 3.10 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python${PYTHON_VERSION} 1

RUN wget -q https://bootstrap.pypa.io/get-pip.py && \
    python${PYTHON_VERSION} get-pip.py && \
    rm get-pip.py

# ── GMP + ChiaVDF from builder ──
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
RUN pip install --upgrade pip setuptools wheel && \
    pip install /tmp/*.whl && \
    rm -rf /tmp/*.whl

# ── Install pinned dependencies first ──
COPY services/miner-api/requirements_v19.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy vLLM v0.19 source from named build context (vllm_src) ──
# Decoupled from the submodule HEAD: the v0.19 worktree is passed as
# --build-context vllm_src=services/miner-api/vllm-v019 so the in-repo submodule can
# stay on feature/pow-on-v0.16 without affecting this build.
COPY --from=vllm_src . /app/vllm
WORKDIR /app/vllm

# ── Extract C extensions + flash_attn Python wrappers from upstream wheel ──
COPY --from=vllm-wheel-fetch /wheels/vllm-${VLLM_VERSION}-*.whl /tmp/vllm.whl
RUN SITE=/usr/local/lib/python${PYTHON_VERSION}/dist-packages && \
    unzip -q /tmp/vllm.whl -d /tmp/vllm-unpacked && \
    mkdir -p ${SITE}/vllm ${SITE}/vllm/vllm_flash_attn && \
    cp /tmp/vllm-unpacked/vllm/*.so ${SITE}/vllm/ && \
    cp -a /tmp/vllm-unpacked/vllm/vllm_flash_attn/. \
       ${SITE}/vllm/vllm_flash_attn/ && \
    rm -rf /tmp/vllm.whl /tmp/vllm-unpacked

WORKDIR /app

# ── Generate FlatBuffers Python files ──
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs && \
    flatc --python validation.fbs && \
    flatc --python blockheader.fbs

# Copy generated flatbuffer files into vllm/sampling/proof/
RUN mkdir -p /app/vllm/vllm/sampling/proof && \
    cp -r proof/* /app/vllm/vllm/sampling/proof/

# ── C++ proof processor from builder ──
COPY --from=proof-processor-builder /build/tests/build/proof_processor.so \
     /usr/local/lib/python${PYTHON_VERSION}/dist-packages/

# ── Copy shared-utils into vllm/sampling/ ──
COPY shared-utils/pow-utils/common_sampler_helper.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_utils.py             /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_v3.py                /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/bcred_table_r1024.py     /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/zmq_pow_writer.py        /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/uint256_arithmetics.py   /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/test/zmq_test_listener.py /app/vllm/vllm/sampling/

# ── Overlay our patched pure-Python files onto installed vllm ──
# The pip-installed vllm has the C extensions (.so); we overlay our
# modified .py files on top, preserving the compiled modules. This is
# also how the HAS_TRITON=False patch (vllm/triton_utils/importing.py)
# lands in the runtime image — the compiled Triton kernels are still
# present in the .so files but are never selected because the dispatch
# wrappers all gate on HAS_TRITON.
RUN rsync -a --exclude='*.so' \
    /app/vllm/vllm/ \
    /usr/local/lib/python${PYTHON_VERSION}/dist-packages/vllm/

# ── Create vllm CLI entrypoint + minimal dist-info ──
RUN printf '#!/usr/bin/python3\nimport sys\nfrom vllm.entrypoints.cli.main import main\nif __name__ == "__main__":\n    sys.exit(main())\n' \
    > /usr/local/bin/vllm && chmod +x /usr/local/bin/vllm && \
    DIST_DIR=/usr/local/lib/python${PYTHON_VERSION}/dist-packages/vllm-${VLLM_VERSION}.dist-info && \
    mkdir -p ${DIST_DIR} && \
    printf 'Metadata-Version: 2.1\nName: vllm\nVersion: %s\n' "${VLLM_VERSION}" > ${DIST_DIR}/METADATA && \
    printf 'vllm\n' > ${DIST_DIR}/top_level.txt && \
    printf '[console_scripts]\nvllm = vllm.entrypoints.cli.main:main\n' > ${DIST_DIR}/entry_points.txt && \
    touch ${DIST_DIR}/INSTALLER

# ── Environment ──
ENV PYTHONPATH=""
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# ── Startup ──
COPY services/miner-api/start.sh /app/
RUN chmod +x /app/start.sh \
 && mkdir -p /models /data/pow_proofs /data/miner_logs \
 && chmod 777 /models /data /data/pow_proofs /data/miner_logs \
 && useradd -m -u 1000 vllm \
 && chown -R vllm:vllm /app /models /data

USER vllm
EXPOSE 8000

ENV MODEL_NAME="Qwen/Qwen3-8B"
ENV VLLM_ENABLE_POW=1

# Labels for traceability
LABEL vllm.version="${VLLM_VERSION}" \
      pow.version="v0.19-port-canonical" \
      cuda.version="${CUDA_VERSION}"

CMD ["/app/start.sh"]
