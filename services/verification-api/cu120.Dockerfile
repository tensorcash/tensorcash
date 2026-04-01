# Multi-stage build for ChiaVDF and vLLM unified container

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
RUN wget https://gmplib.org/download/gmp/gmp-${GMP_VERSION}.tar.xz && \
    tar xf gmp-${GMP_VERSION}.tar.xz && \
    cd gmp-${GMP_VERSION} && \
    ./configure \
      --enable-assembly \
      --enable-shared \
      --enable-static \
      --with-pic && \
    make -j$(nproc) && \
    make install && ldconfig && \
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

# Configure CMake flags for assembly + bench squarer
# ENV CMAKE_ARGS="\
#   -DCMAKE_PREFIX_PATH=/usr/local \
#   -DCMAKE_INCLUDE_PATH=/usr/local/include \
#   -DCMAKE_LIBRARY_PATH=/usr/local/lib \
#   -DSQUARE_ASM=ON \
#   -DENABLE_ASM=ON \
#   -DGMP_USE_ASM=ON \
#   -DFLINT_ENABLE_ASM=ON \
#   -DNASM_EXECUTABLE=/usr/bin/nasm \
#   -DYASM_EXECUTABLE=/usr/bin/yasm \
#   -DCMAKE_BUILD_TYPE=Release \
#   -DCMAKE_CXX_FLAGS=-O3\ -march=native\ -mbmi2\ -mavx2\ -funroll-loops\ -fomit-frame-pointer \
#   -DCMAKE_C_FLAGS=-O3\ -march=native\ -mbmi2\ -mavx2\ -funroll-loops\ -fomit-frame-pointer"

# Build ChiaVDF as a wheel
RUN VERBOSE=1 pip wheel . -w /chiavdf-wheels -v 2>&1 | tee /build.log

# Stage 2: Fetch vLLM wheel
FROM python:3.10-slim as vllm-wheel-fetch
RUN pip install --upgrade pip wheel
RUN pip wheel vllm==0.8.5 --no-deps -w /wheels

# Stage 3: Final unified runtime image
FROM nvidia/cuda:12.0.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install system dependencies including flatbuffers compiler
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-dev python3-pip \
        git unzip ca-certificates rsync wget \
        libboost-all-dev libflint-dev \
        build-essential cmake && \
    rm -rf /var/lib/apt/lists/*
    
# Install FlatBuffers 2.0.0 from official release
RUN wget https://github.com/google/flatbuffers/releases/download/v2.0.0/Linux.flatc.binary.clang++-9.zip && \
    unzip Linux.flatc.binary.clang++-9.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/flatc && \
    rm Linux.flatc.binary.clang++-9.zip

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

# Install system build-deps (ninja) + PyTorch
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ninja-build && pip install --no-cache-dir \
      torch==2.6.0 \
      torchvision==0.21.0 \
      torchaudio==2.6.0

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

# Generate FlatBuffers Python files
WORKDIR /app
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs
RUN flatc --python validation.fbs
RUN flatc --python blockheader.fbs

# Copy generated flatbuffer files to /app/src/utils/proof/
RUN mkdir -p /app/src/utils/proof && \
    cp -r proof/* /app/src/utils/proof/

# Copy pow_utils.py to src/utils/
COPY shared-utils/pow-utils/pow_utils.py /app/src/utils/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/src/utils/
COPY shared-utils/config/constants.py /app/src/config/constants.py 

# Set up environment
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH}"

# Copy application code
WORKDIR /app/src
COPY services/verification-api/src .
COPY services/verification-api/src/config ./config
COPY services/verification-api/src/utils ./utils

CMD ["python", "main.py"]
