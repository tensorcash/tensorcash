# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11
ARG DEBIAN_VERSION=bookworm
ARG CUDA_VERSION=12.3.0
ARG FLATBUFFERS_VERSION=v23.5.26

FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS flatbuffers-builder
ARG FLATBUFFERS_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates pkg-config && \
    rm -rf /var/lib/apt/lists/*

RUN cd /tmp && \
    git clone --depth 1 --branch "${FLATBUFFERS_VERSION}" https://github.com/google/flatbuffers.git && \
    cd flatbuffers && \
    cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DFLATBUFFERS_BUILD_TESTS=OFF \
        -DFLATBUFFERS_BUILD_FLATLIB=ON \
        -DFLATBUFFERS_BUILD_FLATC=ON \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    cmake --build build --config Release -j"$(nproc)" && \
    cmake --install build && \
    ldconfig && \
    rm -rf /tmp/flatbuffers

FROM scratch AS llama-source
COPY services/miner-api/llama.cpp /app/llama.cpp
COPY shared-utils/pow-utils/*.h /app/llama.cpp/tools/server/
COPY shared-utils/pow-utils/*.cpp /app/llama.cpp/tools/server/
COPY shared-utils/fb-schemas/proof.fbs /app/llama.cpp/tools/server/
COPY shared-utils/fb-schemas/blockheader.fbs /app/llama.cpp/tools/server/
COPY shared-utils/fb-schemas/validation.fbs /app/llama.cpp/tools/server/

FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS model-prep

ENV LLAMA_CPP_DIR=/app/llama.cpp
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=llama-source /app/llama.cpp /app/llama.cpp
COPY services/miner-api/prepare_model.py /app/prepare_model.py

RUN pip install --upgrade pip && \
    pip install \
      huggingface_hub \
      transformers \
      -r /app/llama.cpp/requirements/requirements-convert_hf_to_gguf.txt \
      -r /app/llama.cpp/requirements/requirements-convert_hf_to_gguf_update.txt \
      -r /app/llama.cpp/requirements/requirements-convert_llama_ggml_to_gguf.txt \
      -r /app/llama.cpp/requirements/requirements-convert_lora_to_gguf.txt && \
    pip install /app/llama.cpp/gguf-py

ENTRYPOINT ["python3", "/app/prepare_model.py"]

FROM debian:${DEBIAN_VERSION}-slim AS llama-builder-cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake curl git libomp-dev libcurl4-openssl-dev \
        libzmq3-dev libzmq5 cppzmq-dev pkg-config libssl-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=flatbuffers-builder /usr/local /usr/local
COPY --from=llama-source /app /app

RUN ldconfig

WORKDIR /app/llama.cpp

RUN rm -rf build && mkdir -p build && cd build && \
    cmake .. \
        -DLLAMA_SERVER=ON \
        -DGGML_NATIVE=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DGGML_BUILD_EXAMPLES=OFF \
        -DGGML_BUILD_TESTS=OFF \
        -DFlatBuffers_DIR=/usr/local/lib/cmake/flatbuffers \
        -DCMAKE_BUILD_TYPE=Release && \
    cmake --build . --config Release --target llama-server -j"$(nproc)"

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS llama-builder-cuda
ARG CMAKE_CUDA_ARCHITECTURES=""

ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs:/usr/local/cuda/targets/x86_64-linux/lib/stubs \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs:/usr/local/cuda/targets/x86_64-linux/lib/stubs

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake curl git libomp-dev libcurl4-openssl-dev \
        libzmq3-dev libzmq5 pkg-config libssl-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Ubuntu 22.04 CUDA images do not ship a cppzmq package; install the single header directly.
RUN curl -fsSL https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp -o /usr/include/zmq.hpp && \
    ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 && \
    ln -sf /usr/local/cuda/targets/x86_64-linux/lib/stubs/libcuda.so /usr/local/cuda/targets/x86_64-linux/lib/stubs/libcuda.so.1

COPY --from=flatbuffers-builder /usr/local /usr/local
COPY --from=llama-source /app /app

RUN ldconfig

WORKDIR /app/llama.cpp

RUN rm -rf build && mkdir -p build && cd build && \
    linker_flags="-Wl,-rpath-link,/usr/local/cuda/lib64/stubs -Wl,-rpath-link,/usr/local/cuda/targets/x86_64-linux/lib/stubs"; \
    cmake_args="-DLLAMA_SERVER=ON -DGGML_NATIVE=OFF -DGGML_CUDA=ON -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF -DGGML_BUILD_EXAMPLES=OFF -DGGML_BUILD_TESTS=OFF -DFlatBuffers_DIR=/usr/local/lib/cmake/flatbuffers -DCMAKE_BUILD_TYPE=Release"; \
    if [ -n "${CMAKE_CUDA_ARCHITECTURES}" ]; then \
      cmake_args="${cmake_args} -DCMAKE_CUDA_ARCHITECTURES=${CMAKE_CUDA_ARCHITECTURES}"; \
    fi; \
    cmake .. ${cmake_args} "-DCMAKE_EXE_LINKER_FLAGS=${linker_flags}" && \
    cmake --build . --config Release --target llama-server -j"$(nproc)"

FROM debian:${DEBIAN_VERSION}-slim AS llama-cpu

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl python3 libcurl4 libgomp1 libomp5 libssl3 libzmq5 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=llama-builder-cpu /app/llama.cpp/build/bin /tmp/llama-bin
COPY services/miner-api/llama_start.sh /app/start.sh
COPY services/miner-api/llama_supervisor.py /app/llama_supervisor.py
# Canonical Jinja chat templates the supervisor passes to llama-server via
# --chat-template-file when the loaded GGUF's embedded template is missing or
# can't be analyzed by the autoparser (common for community Q4 quants).
COPY services/miner-api/chat-templates /app/chat-templates

RUN chmod +x /app/start.sh && \
    cp /tmp/llama-bin/llama-server /usr/local/bin/llama-server && \
    for lib in /tmp/llama-bin/libllama.so* /tmp/llama-bin/libggml*.so* /tmp/llama-bin/libmtmd.so*; do \
        [ -e "$lib" ] || continue; \
        cp "$lib" /usr/local/lib/; \
    done && \
    rm -rf /tmp/llama-bin && \
    ldconfig

ENV LLAMA_CTX_SIZE=2048 \
    LLAMA_PARALLEL=2 \
    LLAMA_PORT=8000 \
    LLAMA_CONTROL_PORT=8001 \
    LLAMA_CACHE_RAM=0 \
    LLAMA_USE_GPU=0 \
    LLAMA_CHAT_TEMPLATE_DIR=/app/chat-templates

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD curl -f http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/start.sh"]

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04 AS llama-cuda

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 libcurl4 libgomp1 libomp5 libssl3 libzmq5 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=llama-builder-cuda /app/llama.cpp/build/bin /tmp/llama-bin
COPY services/miner-api/llama_start.sh /app/start.sh
COPY services/miner-api/llama_supervisor.py /app/llama_supervisor.py
COPY services/miner-api/chat-templates /app/chat-templates

RUN chmod +x /app/start.sh && \
    cp /tmp/llama-bin/llama-server /usr/local/bin/llama-server && \
    for lib in /tmp/llama-bin/libllama.so* /tmp/llama-bin/libggml*.so* /tmp/llama-bin/libmtmd.so*; do \
        [ -e "$lib" ] || continue; \
        cp "$lib" /usr/local/lib/; \
    done && \
    rm -rf /tmp/llama-bin && \
    ldconfig

ENV LLAMA_CTX_SIZE=2048 \
    LLAMA_PARALLEL=2 \
    LLAMA_PORT=8000 \
    LLAMA_CONTROL_PORT=8001 \
    LLAMA_CACHE_RAM=0 \
    LLAMA_USE_GPU=1 \
    LLAMA_N_GPU_LAYERS=all \
    LLAMA_CHAT_TEMPLATE_DIR=/app/chat-templates \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD curl -f http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/start.sh"]

FROM llama-cpu AS default
