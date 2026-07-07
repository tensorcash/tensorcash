# Multi-stage build for ChiaVDF and vLLM unified container
ARG CUDA_VERSION=12.6.0
ARG CUDA_INDEX=cu126

# Use Ubuntu 22.04 for builder to match runtime environment
FROM ubuntu:22.04 AS chiavdf-builder

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.10 and build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3.10 python3.10-dev python3-pip python3.10-venv \
      nasm yasm build-essential cmake git patch pkg-config \
      libtool autoconf automake wget m4 libboost-all-dev libflint-dev \
      pybind11-dev python3-pybind11 software-properties-common && \
    rm -rf /var/lib/apt/lists/*

# Set Python 3.10 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1

# Install pip for Python 3.10
RUN wget https://bootstrap.pypa.io/get-pip.py && \
    python3.10 get-pip.py && \
    rm get-pip.py

# FROM python:3.10-slim AS chiavdf-builder

# # 1) Build-time deps (including CMake, compiler, etc.)
# RUN apt-get update && \
#     apt-get install -y --no-install-recommends \
#       nasm yasm build-essential cmake git patch pkg-config \
#       libtool autoconf automake wget m4 libboost-all-dev libflint-dev \
#       python3-dev python3-pip pybind11-dev python3-pybind11 && \
#     rm -rf /var/lib/apt/lists/*

# 2) Build GMP 
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

# 3) Build FlatBuffers from source
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

# 4) χiaVDF build 
WORKDIR /opt/chiavdf
COPY shared-utils/chiavdf /opt/chiavdf
RUN git init && git config user.email "build@docker.com" && git config user.name "Docker Build" && \
    git add . && git commit -m "Initial commit" && git tag -a v1.0.0 -m "Version 1.0.0"
RUN pip install --upgrade pip wheel setuptools setuptools_scm pybind11
ENV BUILD_VDF_CLIENT=N
ENV GMP_USE_ASM=1 FLINT_ENABLE_ASM=1 CHIAVDF_NO_ASM=""
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig CMAKE_PREFIX_PATH=/usr/local \
    CMAKE_INCLUDE_PATH=/usr/local/include CMAKE_LIBRARY_PATH=/usr/local/lib 
RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels -v

# 5) Build pfunpack extension **using the FlatBuffers you just installed**
COPY shared-utils/pow-utils/pfunpack/pfunpack.cpp /opt/pfunpack/
COPY shared-utils/pow-utils/pfunpack/CMakeLists.txt  /opt/pfunpack/

WORKDIR /opt/pfunpack
COPY shared-utils/fb-schemas/proof.fbs /opt/pfunpack/
COPY shared-utils/fb-schemas/blockheader.fbs /opt/pfunpack/
COPY shared-utils/fb-schemas/validation.fbs /opt/pfunpack/
RUN flatc --python --cpp proof.fbs
RUN flatc --python --cpp validation.fbs
RUN flatc --python --cpp blockheader.fbs

RUN mkdir build && cd build && \
    cmake .. \
      -DCMAKE_BUILD_TYPE=Release \
      -DPYTHON_EXECUTABLE=$(which python3) && \
    make -j$(nproc)

# Stage 3: Final unified runtime image
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04

ARG CUDA_INDEX=cu126
ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install system dependencies including flatbuffers compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3-pip \
        git unzip ca-certificates wget \
        cmake && \
    rm -rf /var/lib/apt/lists/*
    
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
COPY --from=chiavdf-builder /opt/pfunpack/build/pfunpack.*.so /app/pfunpack.so
RUN ldconfig

# Install ChiaVDF system-wide
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
RUN pip install --upgrade pip setuptools wheel && \
    pip install /tmp/*.whl && \
    rm -rf /tmp/*.whl

# Install PyTorch 2.8.0 stack with CUDA wheels using dynamic CUDA index
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/${CUDA_INDEX} \
      torch==2.8.0 \
      torchvision==0.23.0 \
      torchaudio==2.8.0

# Install verifier requirements
ENV MAX_JOBS=4 
ENV CMAKE_BUILD_PARALLEL_LEVEL=4
COPY services/verification-api/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy verifier code
COPY services/verification-api/src /app/src
WORKDIR /app/src

# Copy vdf tests
COPY shared-utils/chiavdf/tests/test_streaming_verifier.py /app/src/tests/test_streaming_verifier.py 

# Copy FlatBuffers Python files generated in builder stage  
WORKDIR /app
RUN mkdir -p /app/src/utils/proof 
COPY --from=chiavdf-builder /opt/pfunpack/proof /app/src/utils/proof/
COPY --from=chiavdf-builder /opt/pfunpack/proof /app/src/proof/

# Install IPFS (lightweight client configuration)
ARG IPFS_VERSION=v0.25.0
RUN wget -qO /tmp/ipfs.tar.gz "https://dist.ipfs.io/kubo/${IPFS_VERSION}/kubo_${IPFS_VERSION}_linux-amd64.tar.gz" && \
    tar -xzf /tmp/ipfs.tar.gz -C /tmp && \
    mv /tmp/kubo/ipfs /usr/local/bin/ipfs && \
    chmod +x /usr/local/bin/ipfs && \
    rm -rf /tmp/ipfs.tar.gz /tmp/kubo

# Create IPFS data directory for client-only usage
RUN mkdir -p /tmp/ipfs_client && \
    chmod 755 /tmp/ipfs_client

# Environment for lightweight IPFS client
ENV IPFS_PATH=/tmp/ipfs_client

# Copy pow_utils.py to src/utils/
COPY shared-utils/pow-utils/pow_utils.py /app/src/utils/
COPY shared-utils/pow-utils/pow_v3.py /app/src/utils/
# R=1024 B_cred table (§4) — MUST sit beside pow_v3.py (it imports it).
COPY shared-utils/pow-utils/bcred_table_r1024.py /app/src/utils/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/src/utils/
COPY shared-utils/config/constants.py /app/src/config/constants.py 

# Copy only necessary Python files from pow-utils, excluding tests and build artifacts
COPY shared-utils/pow-utils/*.py /app/src/
COPY shared-utils/pow-utils/*.h /app/src/
COPY shared-utils/pow-utils/*.cpp /app/src/
COPY shared-utils/pow-utils/Makefile /app/src/
# Copy proof and proof_mock directories (Python modules)
COPY shared-utils/pow-utils/proof/*.py /app/src/proof/
COPY shared-utils/pow-utils/proof_mock/*.py /app/src/proof_mock/

# Set up environment
ENV PYTHONPATH=""
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# Copy application code
WORKDIR /app/src
COPY services/verification-api/src .
COPY services/verification-api/src/config ./config
COPY services/verification-api/src/utils ./utils

CMD ["python", "main.py"]
