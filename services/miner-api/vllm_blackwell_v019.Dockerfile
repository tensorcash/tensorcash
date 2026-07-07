# vLLM (feature/pow-on-v0.19) built from source — Blackwell (GB10, sm_120).
#
# Structure mirrors vllm_blackwell.Dockerfile (v0.16 variant) — only the
# source branch and version pin change. Same CUDA-13 / sm_120 / arm64 setup.
#
# Why source-build (unchanged rationale): PyPI vllm wheels are built for
# CUDA 12. NVIDIA pytorch containers that ship sm_120 (Blackwell) kernels
# use CUDA 13 (25.09+). Mixing libcudart.so.12 (pip) alongside libcudart.so.13
# (container torch) triggers std::bad_alloc on first GPU allocation.
# Compiling vllm locally against the container's torch+CUDA yields a wheel
# that aligns with the runtime.
#
# v0.19 source delivery: passed via named build context (vllm_src) so the
# in-repo submodule HEAD does not need to move off feature/pow-on-v0.16.
#
#   docker buildx build --builder multiarch --platform linux/arm64 \
#     --build-context vllm_src=services/miner-api/vllm-v019 \
#     -f services/miner-api/vllm_blackwell_v019.Dockerfile \
#     -t ghcr.io/tensorcash/vllm-backend:blackwell-arm64-pow-v19-src \
#     --push \
#     .

ARG CUDA_VERSION=12.9.1
ARG PYTHON_VERSION=3.12

# =============================================================================
# Stage 1: ChiaVDF
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS chiavdf-builder

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
    make -j$(nproc) && make install && \
    { ldconfig || true; } && \
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

ENV GMP_USE_ASM=1 \
    FLINT_ENABLE_ASM=1 \
    CHIAVDF_NO_ASM="" \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    CMAKE_PREFIX_PATH=/usr/local \
    CMAKE_INCLUDE_PATH=/usr/local/include \
    CMAKE_LIBRARY_PATH=/usr/local/lib \
    BUILD_VDF_CLIENT=N

RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels -v 2>&1 | tee /build.log

# =============================================================================
# Stage 2: C++ proof processor (+ arm64-native flatc)
# =============================================================================
# NOTE: must match the RUNTIME Python (pytorch:26.03 = Ubuntu 24.04 / Python 3.12).
# Ubuntu 22.04 ships Python 3.10, which produces a proof_processor.so that the
# 3.12 runtime cannot import ("compiled for Python 3.10") — disabling the C++
# PoW processor. ubuntu24.04 gives Python 3.12 so the .so ABI matches.
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04 AS proof-processor-builder
ARG CUDA_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git wget unzip \
        libzmq3-dev pkg-config \
        python3 python3-dev python3-pip \
        libssl-dev libcrypto++-dev libargon2-dev && \
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp -O /usr/include/zmq.hpp

RUN python3 -m pip install --break-system-packages numpy pybind11

ARG FLATBUFFERS_VERSION=v23.5.26
RUN cd /tmp && \
    git clone --depth 1 --branch ${FLATBUFFERS_VERSION} https://github.com/google/flatbuffers.git && \
    cd flatbuffers && \
    mkdir build && cd build && \
    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DFLATBUFFERS_BUILD_TESTS=OFF \
        -DFLATBUFFERS_BUILD_FLATLIB=ON \
        -DFLATBUFFERS_BUILD_FLATC=ON \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    make -j$(nproc) && \
    make install && \
    { ldconfig || true; } && \
    cd / && rm -rf /tmp/flatbuffers

WORKDIR /build
COPY shared-utils/pow-utils/ ./
COPY shared-utils/fb-schemas/ ./fb-schemas/
RUN FB_SCHEMAS_DIR=/build/fb-schemas bash -c '. tests/build_proofprocessor_simple.sh'

# =============================================================================
# Stage 3: Build vLLM wheel from local POW-on-v0.16 source
# Uses the SAME NVIDIA base as runtime so the built wheel's C extensions
# match the target torch + CUDA ABI exactly.
# =============================================================================
FROM nvcr.io/nvidia/pytorch:26.03-py3 AS vllm-source-build

ENV DEBIAN_FRONTEND=noninteractive
# libboost/libflint/libzmq pull in Ubuntu's libucs.so.0 (lacks
# ucs_config_doc_nop). HPC-X libucc.so.1 otherwise picks it. Fix at build time
# too — otherwise `import torch` inside pip's setup.py hook would die.
RUN echo '/opt/hpcx/ucx/lib' > /etc/ld.so.conf.d/00-hpcx-ucx.conf && ldconfig

WORKDIR /src
# v0.19 source from named build context (decouples from submodule HEAD)
COPY --from=vllm_src . /src/vllm

# Prepare build env: upgrade numpy/scipy first (mistral_common etc. need >=2),
# and install vllm's build requirements. --no-build-isolation below lets the
# build step see the already-installed torch rather than installing 2.9.1
# (which would strip the NVIDIA Blackwell build).
RUN pip install --no-cache-dir --upgrade 'numpy>=2' 'scipy>=1.13' && \
    pip install --no-cache-dir cmake ninja packaging wheel setuptools setuptools_scm pybind11

# Strip the strict torch==2.9.1 pin from vllm's build-time requirements so
# pip --no-build-isolation doesn't try to reinstall torch over NVIDIA's
# 2.9.0a0+nv25.09 build.
RUN sed -i.bak -E '/^(torch|torchvision|torchaudio)[[:space:]=]/d' /src/vllm/requirements/build.txt /src/vllm/requirements/cuda.txt && \
    echo "--- build.txt (torch stripped) ---" && cat /src/vllm/requirements/build.txt

# Target Blackwell (sm_120) — GB10 — plus 12.0+PTX JIT fallback for any
# future compute capabilities. vllm's setup.py reads TORCH_CUDA_ARCH_LIST.
ENV TORCH_CUDA_ARCH_LIST="12.0;12.0+PTX" \
    VLLM_TARGET_DEVICE=cuda \
    MAX_JOBS=4 \
    CCACHE_DIR=/ccache \
    VLLM_INSTALL_PUNICA_KERNELS=0 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_VLLM=0.19.0+pow

WORKDIR /src/vllm
# vllm's setup.py delegates to setuptools-scm which fails without .git history.
# The v0.19 source comes from a host-side git worktree, so its `.git` is a
# gitfile pointing to a host path that doesn't exist inside the container —
# strip it first, then a fresh init + tag gives setuptools-scm something to
# resolve against.
RUN rm -rf .git && \
    git init -q && \
    git config user.email build@docker.com && \
    git config user.name "build" && \
    git add -A && \
    git commit -q -m "blackwell build snapshot" && \
    git tag -a v0.19.0 -m "pow-on-v0.19 snapshot"

# Do not pipe to tail — the shell `| tail` would mask pip's exit code so a
# failed build produces an empty /wheels and stage-4 dies cryptically on the
# glob expansion.
RUN pip wheel --no-deps --no-build-isolation -w /wheels -v .

# =============================================================================
# Stage 4: Final unified runtime image
# =============================================================================
FROM nvcr.io/nvidia/pytorch:26.03-py3
ARG CUDA_VERSION

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git unzip ca-certificates rsync wget \
        libboost-all-dev libflint-dev libzmq3-dev libargon2-1 && \
    rm -rf /var/lib/apt/lists/*

# HPC-X libucs path priority (see Stage 3 comment).
RUN echo '/opt/hpcx/ucx/lib' > /etc/ld.so.conf.d/00-hpcx-ucx.conf && ldconfig

COPY --from=proof-processor-builder /usr/local/bin/flatc /usr/local/bin/flatc
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig || true

# ChiaVDF
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
# pip 26 in pytorch:26.03-py3 refuses to uninstall debian-installed wheel
# (missing RECORD file), so use --ignore-installed to overlay rather than
# uninstall+reinstall. The chiavdf install itself doesn't need an upgraded
# wheel/setuptools — they're transitive deps satisfied by the upgrade.
RUN pip install --upgrade --ignore-installed pip setuptools wheel && \
    pip install /tmp/*.whl && \
    rm -rf /tmp/*.whl

# Numpy 2 + scipy upgrade (same rationale as Stage 3)
RUN pip install --no-cache-dir --upgrade 'numpy>=2' 'scipy>=1.13' flatbuffers

# Install our locally-built vllm wheel (matches container's torch + CUDA).
COPY --from=vllm-source-build /wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-deps /tmp/wheels/vllm-*.whl && rm -rf /tmp/wheels

# Copy POW-on-v0.19 source for runtime overlay + install its non-torch deps.
COPY --from=vllm_src . /app/vllm
# Patch: vllm 0.19 was developed against torch master with `hoist=True`
# kwarg in register_opaque_type. NVIDIA pytorch:26.03 ships torch 2.11.0a0
# (predates the hoist kwarg), so strip it. Optimization hint, not required
# for correctness.
RUN sed -i 's/, hoist=True//g' /app/vllm/vllm/utils/torch_utils.py

# Triton (bundled with torch 2.11) hardcodes /usr/bin/gcc-11 for its
# CudaUtils JIT compile step. pytorch:26.03 is Ubuntu 24.04 with gcc-13
# as default — symlink so triton finds *a* gcc.
RUN ln -sf /usr/bin/gcc /usr/bin/gcc-11
RUN cp /app/vllm/requirements/cuda.txt /app/vllm/requirements/cuda.txt.orig && \
    sed -i.bak -E '/^(torch|torchvision|torchaudio)[[:space:]=]/d' /app/vllm/requirements/cuda.txt && \
    pip install --no-cache-dir -r /app/vllm/requirements/cuda.txt && \
    mv /app/vllm/requirements/cuda.txt.orig /app/vllm/requirements/cuda.txt && \
    rm -f /app/vllm/requirements/cuda.txt.bak

WORKDIR /app
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs && \
    flatc --python validation.fbs && \
    flatc --python blockheader.fbs

RUN mkdir -p /app/vllm/vllm/sampling/proof && \
    cp -r proof/* /app/vllm/vllm/sampling/proof/

RUN python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" > /app/.site_packages
COPY --from=proof-processor-builder /build/tests/build/proof_processor.so /tmp/proof_processor.so
RUN SP=$(cat /app/.site_packages) && \
    install -m 0644 /tmp/proof_processor.so "$SP/proof_processor.so" && \
    rm /tmp/proof_processor.so

COPY shared-utils/pow-utils/common_sampler_helper.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_utils.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_v3.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/bcred_table_r1024.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/zmq_pow_writer.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/test/zmq_test_listener.py /app/vllm/vllm/sampling/

# Overlay POW-on-v0.16 Python onto installed vllm, preserving compiled _C*.so.
RUN SP=$(cat /app/.site_packages) && \
    rsync -a --exclude='_C*.so' --exclude='*.so.*' /app/vllm/vllm/ "$SP/vllm/"

ENV PYTHONPATH="/app"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

COPY services/miner-api/start.sh /app/
RUN chmod +x /app/start.sh \
 && mkdir -p /models && chmod 777 /models \
 && (id -u 1000 >/dev/null 2>&1 || useradd -m -u 1000 vllm) \
 && chown -R 1000:1000 /app /models

USER 1000
EXPOSE 8000

ENV MODEL_NAME="gpt2-large"
ENV VLLM_ENABLE_POW=1

CMD ["/app/start.sh"]
