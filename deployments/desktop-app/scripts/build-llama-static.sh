#!/usr/bin/env bash
# Build llama-server with statically linked dependencies (no Homebrew runtime deps)
# This script builds libzmq and flatbuffers from source for clean bundling

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# Set up build directory with proper absolute path
_default_build_dir="${SCRIPT_DIR}/../build"
BUILD_DIR="${BUILD_DIR:-${_default_build_dir}}"

# Convert BUILD_DIR to absolute path robustly
if [[ "${BUILD_DIR}" != /* ]]; then
    # Relative path - make it absolute from current directory
    BUILD_DIR="$(cd "$(dirname "${BUILD_DIR}")" && pwd)/$(basename "${BUILD_DIR}")"
fi
mkdir -p "${BUILD_DIR}"
BUILD_DIR="$(cd "${BUILD_DIR}" && pwd)"

DEPS_DIR="${BUILD_DIR}/deps"
LLAMA_DIR="${PROJECT_ROOT}/services/miner-api/llama.cpp"

# Verify paths are absolute
if [[ "${BUILD_DIR}" != /* ]] || [[ "${DEPS_DIR}" != /* ]]; then
    echo "ERROR: Failed to compute absolute paths"
    echo "BUILD_DIR: ${BUILD_DIR}"
    echo "DEPS_DIR: ${DEPS_DIR}"
    exit 1
fi

# Versions
LIBZMQ_VERSION="${LIBZMQ_VERSION:-4.3.5}"
FLATBUFFERS_VERSION="${FLATBUFFERS_VERSION:-23.5.26}"

echo "=== Building llama-server with static dependencies ==="
echo "Build directory: ${BUILD_DIR}"
echo "Dependencies: ${DEPS_DIR}"

mkdir -p "${BUILD_DIR}" "${DEPS_DIR}"

# -----------------------------------------------------------------------------
# Build libzmq (static)
# -----------------------------------------------------------------------------
build_libzmq() {
    echo "=== Building libzmq ${LIBZMQ_VERSION} (static) ==="

    if [ -f "${DEPS_DIR}/lib/libzmq.a" ]; then
        echo "libzmq already built, skipping..."
        return
    fi

    cd "${BUILD_DIR}"

    if [ ! -d "libzmq-${LIBZMQ_VERSION}" ]; then
        curl -sL "https://github.com/zeromq/libzmq/releases/download/v${LIBZMQ_VERSION}/zeromq-${LIBZMQ_VERSION}.tar.gz" | tar xz
        mv "zeromq-${LIBZMQ_VERSION}" "libzmq-${LIBZMQ_VERSION}"
    fi

    cd "libzmq-${LIBZMQ_VERSION}"
    mkdir -p build && cd build

    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${DEPS_DIR}" \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DBUILD_SHARED=OFF \
        -DBUILD_STATIC=ON \
        -DWITH_DOCS=OFF \
        -DWITH_LIBSODIUM=OFF \
        -DENABLE_DRAFTS=OFF \
        -DCMAKE_OSX_ARCHITECTURES="arm64"

    cmake --build . --config Release -j$(sysctl -n hw.ncpu)
    cmake --install .

    echo "libzmq built successfully"
}

# -----------------------------------------------------------------------------
# Install cppzmq headers
# -----------------------------------------------------------------------------
install_cppzmq() {
    echo "=== Installing cppzmq headers ==="

    if [ -f "${DEPS_DIR}/include/zmq.hpp" ]; then
        echo "cppzmq already installed, skipping..."
        return
    fi

    curl -sL "https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq.hpp" \
        -o "${DEPS_DIR}/include/zmq.hpp"
    curl -sL "https://raw.githubusercontent.com/zeromq/cppzmq/v4.10.0/zmq_addon.hpp" \
        -o "${DEPS_DIR}/include/zmq_addon.hpp"

    echo "cppzmq headers installed"
}

# -----------------------------------------------------------------------------
# Build flatbuffers
# -----------------------------------------------------------------------------
build_flatbuffers() {
    echo "=== Building flatbuffers ${FLATBUFFERS_VERSION} ==="

    if [ -f "${DEPS_DIR}/bin/flatc" ]; then
        echo "flatbuffers already built, skipping..."
        return
    fi

    cd "${BUILD_DIR}"

    if [ ! -d "flatbuffers-${FLATBUFFERS_VERSION}" ]; then
        curl -sL "https://github.com/google/flatbuffers/archive/refs/tags/v${FLATBUFFERS_VERSION}.tar.gz" | tar xz
    fi

    cd "flatbuffers-${FLATBUFFERS_VERSION}"
    mkdir -p build && cd build

    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${DEPS_DIR}" \
        -DFLATBUFFERS_BUILD_TESTS=OFF \
        -DFLATBUFFERS_BUILD_FLATLIB=ON \
        -DFLATBUFFERS_BUILD_FLATC=ON \
        -DCMAKE_OSX_ARCHITECTURES="arm64"

    cmake --build . --config Release -j$(sysctl -n hw.ncpu)
    cmake --install .

    echo "flatbuffers built successfully"
}

# -----------------------------------------------------------------------------
# Copy shared files to llama.cpp
# -----------------------------------------------------------------------------
prepare_shared_files() {
    echo "=== Preparing shared files ==="

    # Copy PoW headers and source
    cp "${PROJECT_ROOT}/shared-utils/pow-utils/"*.h "${LLAMA_DIR}/tools/server/"
    cp "${PROJECT_ROOT}/shared-utils/pow-utils/"*.cpp "${LLAMA_DIR}/tools/server/"

    # Generate FlatBuffer headers
    "${DEPS_DIR}/bin/flatc" --cpp "${PROJECT_ROOT}/shared-utils/fb-schemas/proof.fbs"
    "${DEPS_DIR}/bin/flatc" --cpp "${PROJECT_ROOT}/shared-utils/fb-schemas/blockheader.fbs"
    "${DEPS_DIR}/bin/flatc" --cpp "${PROJECT_ROOT}/shared-utils/fb-schemas/validation.fbs"

    # Copy generated headers
    cp *_generated.h "${LLAMA_DIR}/tools/server/"

    echo "Shared files prepared"
}

# -----------------------------------------------------------------------------
# Build llama-server
# -----------------------------------------------------------------------------
build_llama_server() {
    echo "=== Building llama-server with Metal ==="

    cd "${LLAMA_DIR}"

    # Clean previous build
    rm -rf build
    mkdir -p build && cd build

    # Configure with static linking
    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_METAL=ON \
        -DLLAMA_SERVER=ON \
        -DBUILD_SHARED_LIBS=OFF \
        -DCMAKE_PREFIX_PATH="${DEPS_DIR}" \
        -DCMAKE_INCLUDE_PATH="${DEPS_DIR}/include" \
        -DCMAKE_LIBRARY_PATH="${DEPS_DIR}/lib" \
        -DCMAKE_EXE_LINKER_FLAGS="-L${DEPS_DIR}/lib -lzmq -framework CoreFoundation -framework Security" \
        -DCMAKE_FIND_LIBRARY_SUFFIXES=".a" \
        -DCMAKE_OSX_ARCHITECTURES="arm64"

    cmake --build . --target llama-server --config Release -j$(sysctl -n hw.ncpu)

    echo "llama-server built successfully"
}

# -----------------------------------------------------------------------------
# Package output (bundle dylibs and fix paths)
# -----------------------------------------------------------------------------
package_output() {
    echo "=== Packaging output ==="

    OUTPUT_DIR="${BUILD_DIR}/output"
    LIBS_DIR="${OUTPUT_DIR}/libs"
    mkdir -p "${OUTPUT_DIR}" "${LIBS_DIR}"

    cp "${LLAMA_DIR}/build/bin/llama-server" "${OUTPUT_DIR}/"

    echo "Checking library dependencies..."
    otool -L "${OUTPUT_DIR}/llama-server" | head -20

    # Bundle Homebrew dependencies and fix paths
    echo "Bundling external dependencies..."

    # Find and bundle Homebrew dylibs
    for dylib in $(otool -L "${OUTPUT_DIR}/llama-server" | grep -E "/opt/homebrew|/usr/local/Cellar" | awk '{print $1}'); do
        echo "Bundling: ${dylib}"
        dylib_name=$(basename "${dylib}")

        # Copy the dylib
        cp "${dylib}" "${LIBS_DIR}/"
        chmod 755 "${LIBS_DIR}/${dylib_name}"

        # Fix the reference in llama-server
        install_name_tool -change "${dylib}" "@executable_path/libs/${dylib_name}" "${OUTPUT_DIR}/llama-server"

        # Also check for dependencies of the bundled dylib
        for subdep in $(otool -L "${LIBS_DIR}/${dylib_name}" | grep -E "/opt/homebrew|/usr/local/Cellar" | awk '{print $1}'); do
            subdep_name=$(basename "${subdep}")
            if [ ! -f "${LIBS_DIR}/${subdep_name}" ]; then
                echo "  Bundling subdep: ${subdep}"
                cp "${subdep}" "${LIBS_DIR}/"
                chmod 755 "${LIBS_DIR}/${subdep_name}"
            fi
            # Fix reference in this dylib
            install_name_tool -change "${subdep}" "@executable_path/libs/${subdep_name}" "${LIBS_DIR}/${dylib_name}"
        done

        # Fix the dylib's own ID
        install_name_tool -id "@executable_path/libs/${dylib_name}" "${LIBS_DIR}/${dylib_name}"
    done

    # Verify no remaining Homebrew dependencies
    echo ""
    echo "Final dependency check for llama-server:"
    otool -L "${OUTPUT_DIR}/llama-server" | head -20

    if otool -L "${OUTPUT_DIR}/llama-server" | grep -q "/opt/homebrew\|/usr/local/Cellar"; then
        echo "ERROR: Still found Homebrew dependencies after bundling!"
        exit 1
    fi

    echo ""
    echo "Output packaged to: ${OUTPUT_DIR}/"
    ls -la "${OUTPUT_DIR}/"
    ls -la "${LIBS_DIR}/" 2>/dev/null || true
}

# -----------------------------------------------------------------------------
# Sign binaries (ad-hoc or with Developer ID if available)
# -----------------------------------------------------------------------------
sign_binaries() {
    echo "=== Signing binaries ==="

    # Clear any quarantine/provenance attributes
    xattr -cr "${OUTPUT_DIR}/llama-server" "${LIBS_DIR}"/* 2>/dev/null || true

    # Check if we have a signing identity
    SIGN_IDENTITY="${CODESIGN_IDENTITY:--}"

    # Sign bundled dylibs first (must sign dependencies before main binary)
    for dylib in "${LIBS_DIR}"/*.dylib; do
        if [ -f "$dylib" ]; then
            echo "Signing: $(basename "$dylib")"
            codesign -s "${SIGN_IDENTITY}" --force --timestamp "${dylib}" 2>/dev/null || \
            codesign -s - --force "${dylib}"
        fi
    done

    # Sign main binary
    echo "Signing: llama-server"
    codesign -s "${SIGN_IDENTITY}" --force --timestamp "${OUTPUT_DIR}/llama-server" 2>/dev/null || \
    codesign -s - --force "${OUTPUT_DIR}/llama-server"

    # Verify signature
    echo ""
    echo "Verifying signatures..."
    codesign -v "${OUTPUT_DIR}/llama-server" && echo "llama-server: OK" || echo "llama-server: FAILED"

    echo "Signing complete"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    build_libzmq
    install_cppzmq
    build_flatbuffers
    prepare_shared_files
    build_llama_server
    package_output
    sign_binaries

    echo "=== Build complete ==="
    echo "llama-server: ${BUILD_DIR}/output/llama-server"
}

main "$@"
