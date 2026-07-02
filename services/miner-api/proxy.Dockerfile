# Stage 1: Build ChiaVDF with assembly optimizations
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
    CHIAVDF_NO_ASM=""

# ChiaVDF CMakeLists.txt defaults -march=native, which bakes the builder host's
# ISA into the .so and then SIGILLs on any RUN host that lacks it. Pin a
# portable per-arch baseline instead (CHIAVDF_MARCH is documented in
# src/CMakeLists.txt:30). x86-64-v3 is meaningless on ARM, so building on an
# aarch64 host (e.g. Apple Silicon docker) with it produces a broken/no-op VDF
# lib — the arch is detected at build time below, right before the wheel build.
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    CMAKE_PREFIX_PATH=/usr/local \
    CMAKE_INCLUDE_PATH=/usr/local/include \
    CMAKE_LIBRARY_PATH=/usr/local/lib
ENV BUILD_VDF_CLIENT=N


# Portable -march per target arch (see note above). x86-64-v3 = AVX2+BMI2+FMA+
# F16C (every Haswell+ we deploy on); armv8-a = ARMv8 baseline for aarch64.
RUN case "$(uname -m)" in \
      x86_64)        export CHIAVDF_MARCH=x86-64-v3 ;; \
      aarch64|arm64) export CHIAVDF_MARCH=armv8-a ;; \
      *)             export CHIAVDF_MARCH=native ;; \
    esac; \
    echo "chiavdf: building with -march=${CHIAVDF_MARCH} on $(uname -m)"; \
    VERBOSE=1 pip wheel . -w /chiavdf-wheels

# Stage 2: Runtime image for Mining Proxy
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install only the system tools needed for flatc and your app
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      git unzip ca-certificates rsync wget curl \
      libboost-all-dev libflint-dev \
      build-essential cmake && \
    rm -rf /var/lib/apt/lists/*

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

# Ensure pip is up-to-date
RUN pip install --upgrade pip

# Copy GMP libs & headers from builder
COPY --from=chiavdf-builder /usr/local/lib/libgmp* /usr/local/lib/
COPY --from=chiavdf-builder /usr/local/include/gmp* /usr/local/include/
RUN ldconfig

# Install the ChiaVDF wheel
COPY --from=chiavdf-builder /chiavdf-wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

# Copy and install your Python dependencies
COPY services/miner-api/proxy_requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Generate FlatBuffers Python files
WORKDIR /app
COPY shared-utils/fb-schemas/proof.fbs /app/
COPY shared-utils/fb-schemas/blockheader.fbs /app/
COPY shared-utils/fb-schemas/validation.fbs /app/
RUN flatc --python proof.fbs
RUN flatc --python validation.fbs
RUN flatc --python blockheader.fbs

# Move generated code into your src layout
RUN mkdir -p /app/src/proof 
RUN mkdir -p /bcore_data
RUN cp -r /app/proof/* /app/src/proof/

# Copy any helper utils
COPY shared-utils/pow-utils/pow_utils.py /app/src/utils/
COPY shared-utils/pow-utils/uint256_arithmetics.py /app/src/utils/uint256_arithmetics.py
COPY shared-utils/chiavdf/tests/test_streaming_verifier.py /app/src/test_streaming_verifier.py 
COPY shared-utils/config/constants.py /app/src/config/constants.py 

# Copy application code
WORKDIR /app/src
COPY services/miner-api/src/main.py .
COPY services/miner-api/src/worker_client.py .
COPY services/miner-api/src/components ./components

EXPOSE 8080 6000

CMD ["python", "main.py"]
