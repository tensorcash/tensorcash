# Multi-stage build for ChiaVDF and vLLM unified container

# Build arguments (override via --build-arg)
ARG CUDA_VERSION=12.3.0
ARG VLLM_VERSION=0.10.0

# Stage 1: Build ChiaVDF with assembly optimizations
FROM python:3.10-slim AS chiavdf-builder

# Install build-time dependencies for ChiaVDF
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

# Clone ChiaVDF sources
WORKDIR /opt
COPY shared-utils/chiavdf /opt/chiavdf

# Prepare Python build environment
WORKDIR /opt/chiavdf
RUN git init && \
    git config user.email "build@docker.com" && \
    git config user.name "Docker Build" && \
    git add . && \
    git commit -m "Initial commit" && \
    git tag -a v1.0.0 -m "Version 1.0.0"
RUN pip install --upgrade pip wheel setuptools setuptools_scm pybind11

# Enable all asm paths
ENV GMP_USE_ASM=1
ENV FLINT_ENABLE_ASM=1
ENV CHIAVDF_NO_ASM=""

# Configure pkg-config and CMake paths
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
ENV CMAKE_PREFIX_PATH=/usr/local
ENV CMAKE_INCLUDE_PATH=/usr/local/include
ENV CMAKE_LIBRARY_PATH=/usr/local/lib
ENV BUILD_VDF_CLIENT=N

# Build ChiaVDF as a wheel
RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels -v 2>&1 | tee /build.log

# Stage 2: Build C++ proof processor
# Use same base as runtime for GLIBC compatibility
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04 AS proof-processor-builder
ARG CUDA_VERSION

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git wget unzip \
        libzmq3-dev pkg-config \
        python3.10 python3.10-dev python3-pip \
        libssl-dev libcrypto++-dev libargon2-dev && \
    rm -rf /var/lib/apt/lists/*

# Install cppzmq header (single header file, not packaged in Ubuntu 22.04)
RUN wget -q https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp -O /usr/include/zmq.hpp

# Install Python build dependencies
RUN python3.10 -m pip install --upgrade pip numpy pybind11

# Build and install FlatBuffers from source (following llama.Dockerfile pattern)
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
    ldconfig && \
    cd / && rm -rf /tmp/flatbuffers

# Build proof processor
WORKDIR /build
COPY shared-utils/pow-utils/ ./
COPY shared-utils/fb-schemas/ ./fb-schemas/
RUN FB_SCHEMAS_DIR=/build/fb-schemas bash -c '. tests/build_proofprocessor_simple.sh'

# Stage 3: Fetch vLLM wheel
FROM python:3.10-slim AS vllm-wheel-fetch
ARG VLLM_VERSION
RUN pip install --upgrade pip wheel && \
    pip wheel vllm==${VLLM_VERSION} --no-deps -w /wheels


# Stage 4: Final unified runtime image
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04
ARG CUDA_VERSION 
ARG VLLM_VERSION

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install system runtime dependencies (removed build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-dev python3-pip \
        git unzip ca-certificates rsync wget \
        libboost-all-dev libflint-dev libzmq3-dev libargon2-1 && \
    rm -rf /var/lib/apt/lists/*
    
# Install FlatBuffers from official release (still needed for runtime FlatBuffer generation)
RUN wget https://github.com/google/flatbuffers/releases/download/v23.5.26/Linux.flatc.binary.g%2B%2B-10.zip && \
    unzip Linux.flatc.binary.g++-10.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/flatc && \
    rm Linux.flatc.binary.g++-10.zip

# Set Python 3.10 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1

# Install pip
RUN wget https://bootstrap.pypa.io/get-pip.py && \
    python3.10 get-pip.py && \
    rm get-pip.py

# Copy GMP libraries from builder
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig

# Install ChiaVDF system-wide
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
RUN pip install --upgrade pip setuptools wheel && \
    pip install /tmp/*.whl && \
    rm -rf /tmp/*.whl

# Install vLLM requirements
COPY services/miner-api/requirements_v10.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the v0.10 vLLM submodule (pinned at services/miner-api/vllm-v010)
COPY services/miner-api/vllm-v010 /app/vllm
WORKDIR /app/vllm

# Generate FlatBuffers Python files
WORKDIR /app
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs
RUN flatc --python validation.fbs
RUN flatc --python blockheader.fbs

# Copy generated flatbuffer files to vllm/sampling/proof
RUN mkdir -p /app/vllm/vllm/sampling/proof && \
    cp -r proof/* /app/vllm/vllm/sampling/proof/

# Copy built C++ proof processor module from builder
COPY --from=proof-processor-builder /build/tests/build/proof_processor.so /usr/local/lib/python3.10/dist-packages/

# Copy pow_utils.py to vllm/sampling
COPY shared-utils/pow-utils/common_sampler_helper.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_utils.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/pow_v3.py /app/vllm/vllm/sampling/
COPY shared-utils/pow-utils/bcred_table_r1024.py /app/vllm/vllm/sampling/
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

# Set up environment
ENV PYTHONPATH=""
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# Set up environment
COPY services/miner-api/start.sh /app/
COPY services/miner-api/vllm_supervisor.py /app/
RUN chmod +x /app/start.sh \
 && chmod +x /app/vllm_supervisor.py \
 && mkdir -p /models && chmod 777 /models \
 && useradd -m -u 1000 vllm \
 && chown -R vllm:vllm /app /models

USER vllm
EXPOSE 8000

# Environment variables
ENV MODEL_NAME="gpt2-large"
ENV VLLM_ENABLE_POW=1

CMD ["/app/start.sh"]
