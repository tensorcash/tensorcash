#!/usr/bin/env bash
# =============================================================================
# TensorCash Windows Build Script (Cross-Compile)
# =============================================================================
#
# Cross-compiles TensorCash for Windows from Linux or macOS using mingw-w64.
#
# Usage:
#   ./build-windows.sh [options]
#
# Options:
#   --release       Build in release mode (default)
#   --debug         Build in debug mode
#   --arch ARCH     Target: x86_64 or i686 (default: x86_64)
#   --output DIR    Output directory (default: ./dist)
#   --skip-deps     Skip dependency builds
#   --configure-only Stop after CMake configure (fail fast)
#   --clean         Clean build directories first
#
# Requirements:
#   - mingw-w64 toolchain (apt install mingw-w64 or brew install mingw-w64)
#   - CMake 3.22+
#   - NSIS (apt install nsis or brew install nsis)
#   - Rust with windows target (rustup target add x86_64-pc-windows-gnu)
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BCORE_DIR="${REPO_ROOT}/services/core-node/bcore"
COSIGN_DIR="${REPO_ROOT}/services/core-node/cosign-bridge"

# Defaults
BUILD_TYPE="Release"
TARGET_ARCH="x86_64"
OUTPUT_DIR="${SCRIPT_DIR}/dist"
SKIP_DEPS=false
CONFIGURE_ONLY=false
CLEAN=false
BUILD_JOBS="${BUILD_JOBS:-4}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

if ! [[ "${BUILD_JOBS}" =~ ^[1-9][0-9]*$ ]]; then
    log_error "BUILD_JOBS must be a positive integer, got: ${BUILD_JOBS}"
    exit 1
fi

# Sanity check for required sources before computing version info
if [[ ! -f "${BCORE_DIR}/CMakeLists.txt" ]]; then
    log_error "Missing ${BCORE_DIR}/CMakeLists.txt (bcore submodule not initialized)."
    log_error "Run: git submodule update --init --recursive services/core-node/bcore"
    exit 1
fi

# App metadata — derive from DEFAULT_CHAIN_TYPE (set by CI matrix or manually)
DEFAULT_CHAIN_TYPE="${DEFAULT_CHAIN_TYPE:-tensor}"
case "${DEFAULT_CHAIN_TYPE}" in
    tensor-test) APP_NAME="TensorCash-Testnet" ;;
    *)           APP_NAME="TensorCash" ;;
esac
APP_VERSION="$(grep 'CLIENT_VERSION_MAJOR' "${BCORE_DIR}/CMakeLists.txt" | head -1 | grep -oE '[0-9]+')"
APP_VERSION+=".$(grep 'CLIENT_VERSION_MINOR' "${BCORE_DIR}/CMakeLists.txt" | head -1 | grep -oE '[0-9]+')"
APP_VERSION+=".$(grep 'CLIENT_VERSION_BUILD' "${BCORE_DIR}/CMakeLists.txt" | head -1 | grep -oE '[0-9]+')"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --release) BUILD_TYPE="Release"; shift ;;
        --debug) BUILD_TYPE="Debug"; shift ;;
        --arch) TARGET_ARCH="$2"; shift 2 ;;
        --output) OUTPUT_DIR="$2"; shift 2 ;;
        --skip-deps) SKIP_DEPS=true; shift ;;
        --configure-only) CONFIGURE_ONLY=true; shift ;;
        --clean) CLEAN=true; shift ;;
        -h|--help)
            head -28 "$0" | tail -23
            exit 0
            ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Set toolchain based on arch
case "${TARGET_ARCH}" in
    x86_64)
        MINGW_PREFIX="x86_64-w64-mingw32"
        RUST_TARGET="x86_64-pc-windows-gnu"
        ;;
    i686)
        MINGW_PREFIX="i686-w64-mingw32"
        RUST_TARGET="i686-pc-windows-gnu"
        ;;
    *)
        log_error "Unknown architecture: ${TARGET_ARCH}"
        exit 1
        ;;
esac

# Check for mingw
if ! command -v "${MINGW_PREFIX}-gcc" &>/dev/null; then
    log_error "mingw-w64 not found. Install with:"
    log_error "  Ubuntu/Debian: sudo apt install mingw-w64"
    log_error "  macOS: brew install mingw-w64"
    exit 1
fi

# Setup directories
BUILD_DIR="${BCORE_DIR}/build-windows-${TARGET_ARCH}"
STAGING_DIR="${OUTPUT_DIR}/staging-windows"
INSTALL_DIR="${STAGING_DIR}/${APP_NAME}"

if [[ "${CLEAN}" == true ]]; then
    log_info "Cleaning build directories..."
    rm -rf "${BUILD_DIR}" "${STAGING_DIR}"
fi

mkdir -p "${BUILD_DIR}" "${INSTALL_DIR}" "${OUTPUT_DIR}"
log_info "Using BUILD_JOBS=${BUILD_JOBS}"

# =============================================================================
# Fail-fast: Copy icon immediately (no point building for 40 mins if this fails)
# =============================================================================
ICON_SRC="${BCORE_DIR}/src/qt/res/icons/bitcoin.ico"
ICON_DST="${INSTALL_DIR}/TensorCash.ico"
if [[ ! -f "${ICON_SRC}" ]]; then
    log_error "Icon not found: ${ICON_SRC}"
    log_error "Ensure bcore submodule is fully initialized with src/qt/res/icons/"
    exit 1
fi
cp "${ICON_SRC}" "${ICON_DST}"
log_info "Icon copied: ${ICON_DST}"

# Create CMake toolchain file
TOOLCHAIN_FILE="${BUILD_DIR}/mingw-toolchain.cmake"
cat > "${TOOLCHAIN_FILE}" << EOF
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR ${TARGET_ARCH})

set(CMAKE_C_COMPILER ${MINGW_PREFIX}-gcc)
set(CMAKE_CXX_COMPILER ${MINGW_PREFIX}-g++)
set(CMAKE_RC_COMPILER ${MINGW_PREFIX}-windres)

set(CMAKE_FIND_ROOT_PATH /usr/${MINGW_PREFIX})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)

set(CMAKE_EXE_LINKER_FLAGS "-static-libgcc -static-libstdc++ -static")
EOF

# =============================================================================
# Step 1: Build cross-compiled dependencies
# =============================================================================

DEPENDS_DIR="${BCORE_DIR}/depends"
DEFAULT_DEPENDS_PREFIX="${DEPENDS_DIR}/${MINGW_PREFIX}"

# In configure-only mode, don't spend time building deps; require them to exist
if [[ "${CONFIGURE_ONLY}" == true ]]; then
    SKIP_DEPS=true
    if [[ ! -d "${DEFAULT_DEPENDS_PREFIX}" ]]; then
        log_error "Configure-only requested but no depends output found at ${DEFAULT_DEPENDS_PREFIX}."
        log_error "Restore the cache or run a full build once to populate depends."
        exit 1
    fi
fi

if [[ "${SKIP_DEPS}" != true ]]; then
    log_info "Building cross-compiled dependencies..."

    # If bcore has a depends system like Bitcoin Core, use it
    if [[ -f "${DEPENDS_DIR}/Makefile" ]]; then
        log_info "Using depends system..."
        make -C "${DEPENDS_DIR}" HOST="${MINGW_PREFIX}" -j"${BUILD_JOBS}"
        DEPENDS_PREFIX="${DEPENDS_DIR}/${MINGW_PREFIX}"
    else
        log_warn "No depends system found. You may need to provide pre-built Windows libraries."
        log_warn "Consider setting up a depends/ directory following Bitcoin Core's pattern."
    fi
fi

# Copy FlatBuffers headers into depends prefix (must happen AFTER depends make)
if [[ -d "/usr/local/include/flatbuffers" ]]; then
    mkdir -p "${DEPENDS_DIR}/${MINGW_PREFIX}/include"
    cp -r /usr/local/include/flatbuffers "${DEPENDS_DIR}/${MINGW_PREFIX}/include/"
fi

# Install cppzmq headers (depends builds libzmq but not the C++ wrapper)
if [[ ! -f "${DEPENDS_DIR}/${MINGW_PREFIX}/include/zmq.hpp" ]]; then
    log_info "Installing cppzmq headers..."
    CPPZMQ_VER="4.10.0"
    curl -sL "https://github.com/zeromq/cppzmq/archive/refs/tags/v${CPPZMQ_VER}.tar.gz" | tar xz -C /tmp
    mkdir -p "${DEPENDS_DIR}/${MINGW_PREFIX}/include"
    cp /tmp/cppzmq-${CPPZMQ_VER}/zmq.hpp "${DEPENDS_DIR}/${MINGW_PREFIX}/include/"
    cp /tmp/cppzmq-${CPPZMQ_VER}/zmq_addon.hpp "${DEPENDS_DIR}/${MINGW_PREFIX}/include/"
    rm -rf /tmp/cppzmq-${CPPZMQ_VER}
fi

# Default depends prefix (used even when skipping rebuild)
DEPENDS_PREFIX="${DEPENDS_PREFIX:-${DEFAULT_DEPENDS_PREFIX}}"

# =============================================================================
# Step 2: Configure bitcoin-qt for Windows (fast-fail)
# =============================================================================

log_info "Configuring Windows build..."

# Generate FlatBuffers headers
flatc --cpp -o "${BCORE_DIR}/src/rpc" \
    "${REPO_ROOT}/shared-utils/fb-schemas/proof.fbs" \
    "${REPO_ROOT}/shared-utils/fb-schemas/blockheader.fbs" \
    "${REPO_ROOT}/shared-utils/fb-schemas/validation.fbs"

# Prefer the depends-generated toolchain when available (it's designed for the depends packages)
DEPENDS_TOOLCHAIN="${DEPENDS_PREFIX}/toolchain.cmake"
if [[ -n "${DEPENDS_PREFIX:-}" && -f "${DEPENDS_TOOLCHAIN}" ]]; then
    log_info "Using depends-generated toolchain: ${DEPENDS_TOOLCHAIN}"
    EFFECTIVE_TOOLCHAIN="${DEPENDS_TOOLCHAIN}"
else
    log_info "Using custom mingw toolchain: ${TOOLCHAIN_FILE}"
    EFFECTIVE_TOOLCHAIN="${TOOLCHAIN_FILE}"
fi

CMAKE_ARGS=(
    --toolchain "${EFFECTIVE_TOOLCHAIN}"
    -DCMAKE_BUILD_TYPE="${BUILD_TYPE}"
    -DDEFAULT_CHAIN_TYPE="${DEFAULT_CHAIN_TYPE}"
    -DBUILD_DAEMON=OFF
    -DBUILD_CLI=ON
    -DBUILD_GUI=ON
    -DBUILD_TESTS=OFF
    -DBUILD_BENCH=OFF
    -DWITH_ZMQ=ON
    -DENABLE_WALLET=ON
    -DWITH_QRENCODE=ON
)

# Add depends prefix if available
if [[ -n "${DEPENDS_PREFIX:-}" && -d "${DEPENDS_PREFIX}" ]]; then
    # When using custom toolchain (not depends-generated), add search paths
    if [[ "${EFFECTIVE_TOOLCHAIN}" == "${TOOLCHAIN_FILE}" ]]; then
        CMAKE_ARGS+=(-DCMAKE_FIND_ROOT_PATH="${DEPENDS_PREFIX};/usr/${MINGW_PREFIX}")
    fi
    CMAKE_ARGS+=(-DCMAKE_PREFIX_PATH="${DEPENDS_PREFIX}")

    SQLITE_INC="${DEPENDS_PREFIX}/include"
    SQLITE_LIB="$(find "${DEPENDS_PREFIX}"/lib* -maxdepth 1 -type f -name 'libsqlite3*.a' | head -n1 || true)"
    if [[ -z "${SQLITE_LIB}" ]]; then
        log_error "SQLite3 not found in depends output (expected libsqlite3*.a under ${DEPENDS_PREFIX}/lib*)."
        log_error "Ensure depends was built successfully with HOST=${MINGW_PREFIX}."
        exit 1
    fi
    QRENCODE_INC="${DEPENDS_PREFIX}/include"
    QRENCODE_LIB="$(find "${DEPENDS_PREFIX}"/lib* -maxdepth 1 -type f -name 'libqrencode*.a' | head -n1 || true)"
    if [[ -z "${QRENCODE_LIB}" ]]; then
        log_error "QRencode not found in depends output (expected libqrencode*.a under ${DEPENDS_PREFIX}/lib*)."
        log_error "Ensure depends was built successfully with HOST=${MINGW_PREFIX}."
        exit 1
    fi
    # Pass library paths explicitly for both RELEASE and non-suffixed variants
    # to ensure FindQRencode.cmake finds them regardless of search mode
    CMAKE_ARGS+=(
        -DSQLite3_LIBRARY="${SQLITE_LIB}"
        -DSQLite3_INCLUDE_DIR="${SQLITE_INC}"
        -DSQLite3_ROOT="${DEPENDS_PREFIX}"
        -DQRencode_LIBRARY_RELEASE="${QRENCODE_LIB}"
        -DQRencode_LIBRARY="${QRENCODE_LIB}"
        -DQRencode_INCLUDE_DIR="${QRENCODE_INC}"
        -DQRencode_ROOT="${DEPENDS_PREFIX}"
    )
else
    CMAKE_ARGS+=(-DCMAKE_FIND_ROOT_PATH="/usr/${MINGW_PREFIX}")
fi

# Depends Boost is header-only; create stub libraries so FindBoost
# can locate the required COMPONENTS (locale, thread, etc.).
if [[ -n "${DEPENDS_PREFIX:-}" && -d "${DEPENDS_PREFIX}" ]]; then
    BOOST_LIB_DIR="${DEPENDS_PREFIX}/lib"
    mkdir -p "${BOOST_LIB_DIR}"
    echo "" | ${CC:-${MINGW_PREFIX}-gcc-posix} -x c -c -o /tmp/empty_boost.o -
    for lib in locale thread chrono atomic date_time system container; do
        ${MINGW_PREFIX}-ar rcs "${BOOST_LIB_DIR}/libboost_${lib}.a" /tmp/empty_boost.o 2>/dev/null || \
        ar rcs "${BOOST_LIB_DIR}/libboost_${lib}.a" /tmp/empty_boost.o
    done
    rm -f /tmp/empty_boost.o
fi

# Build zstd for Windows cross-compile (not in depends system)
if [[ -n "${DEPENDS_PREFIX:-}" && ! -f "${DEPENDS_PREFIX}/lib/libzstd.a" ]]; then
    log_info "Building zstd for Windows cross-compile..."
    rm -rf /tmp/zstd-win-build
    curl -sL https://github.com/facebook/zstd/releases/download/v1.5.6/zstd-1.5.6.tar.gz | tar xz -C /tmp
    mv /tmp/zstd-1.5.6 /tmp/zstd-win-build
    cd /tmp/zstd-win-build
    make CC="${MINGW_PREFIX}-gcc-posix" AR="${MINGW_PREFIX}-ar" \
         RANLIB="${MINGW_PREFIX}-ranlib" lib-release -j"${BUILD_JOBS}"
    mkdir -p "${DEPENDS_PREFIX}/lib" "${DEPENDS_PREFIX}/include"
    cp lib/libzstd.a "${DEPENDS_PREFIX}/lib/"
    cp lib/zstd.h lib/zstd_errors.h lib/zdict.h "${DEPENDS_PREFIX}/include/"
    # Create pkg-config file so FindPkgConfig can locate it
    mkdir -p "${DEPENDS_PREFIX}/lib/pkgconfig"
    cat > "${DEPENDS_PREFIX}/lib/pkgconfig/libzstd.pc" <<ZSTDPC
prefix=${DEPENDS_PREFIX}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: zstd
Description: fast lossless compression algorithm library
Version: 1.5.6
Libs: -L\${libdir} -lzstd
Cflags: -I\${includedir}
ZSTDPC
    rm -rf /tmp/zstd-win-build
    cd "${REPO_ROOT}"
fi

# Build blst for Windows cross-compile
if [[ -n "${DEPENDS_PREFIX:-}" && ! -f "${DEPENDS_PREFIX}/lib/libblst.a" ]]; then
    log_info "Building blst for Windows cross-compile..."
    rm -rf /tmp/blst-win
    git clone --depth 1 --branch v0.3.11 https://github.com/supranational/blst.git /tmp/blst-win
    cd /tmp/blst-win
    # blst's build.sh doesn't support cross-compile well, build manually
    CC="${MINGW_PREFIX}-gcc-posix"
    # Compile C sources
    ${CC} -O2 -fno-builtin -fPIC -c src/server.c -o server.o -I bindings -I src
    # Use portable assembly (no platform-specific asm for Windows cross-compile)
    ${CC} -O2 -fno-builtin -fPIC -c build/win64/add_mod_256-x86_64.asm -o /dev/null 2>/dev/null || true
    # Build with portable C fallback
    ${CC} -O2 -fno-builtin -fPIC -D__BLST_PORTABLE__ -c src/server.c -o server.o -I bindings -I src
    ${MINGW_PREFIX}-ar rcs libblst.a server.o
    mkdir -p "${DEPENDS_PREFIX}/lib" "${DEPENDS_PREFIX}/include"
    cp libblst.a "${DEPENDS_PREFIX}/lib/"
    cp bindings/blst.h bindings/blst_aux.h "${DEPENDS_PREFIX}/include/"
    cp bindings/*.hpp "${DEPENDS_PREFIX}/include/" 2>/dev/null || true
    rm -rf /tmp/blst-win
    cd "${REPO_ROOT}"
fi

# Build GMP for Windows cross-compile
if [[ -n "${DEPENDS_PREFIX:-}" && ! -f "${DEPENDS_PREFIX}/lib/libgmp.a" ]]; then
    log_info "Building GMP for Windows cross-compile..."
    rm -rf /tmp/gmp-win-build
    curl -sL https://ftp.gnu.org/gnu/gmp/gmp-6.3.0.tar.xz | tar xJ -C /tmp
    mv /tmp/gmp-6.3.0 /tmp/gmp-win-build
    cd /tmp/gmp-win-build
    ./configure \
        --host="${MINGW_PREFIX}" \
        --prefix="${DEPENDS_PREFIX}" \
        --enable-cxx \
        --disable-shared \
        CC="${MINGW_PREFIX}-gcc-posix" \
        CXX="${MINGW_PREFIX}-g++-posix"
    make -j"${BUILD_JOBS}"
    make install
    rm -rf /tmp/gmp-win-build
    cd "${REPO_ROOT}"
fi

# Build OpenSSL for Windows cross-compile
if [[ -n "${DEPENDS_PREFIX:-}" && ! -f "${DEPENDS_PREFIX}/lib/libssl.a" ]]; then
    log_info "Building OpenSSL for Windows cross-compile..."
    rm -rf /tmp/openssl-win-build
    curl -sL https://github.com/openssl/openssl/releases/download/openssl-3.3.1/openssl-3.3.1.tar.gz | tar xz -C /tmp
    mv /tmp/openssl-3.3.1 /tmp/openssl-win-build
    cd /tmp/openssl-win-build
    ./Configure mingw64 \
        --cross-compile-prefix="${MINGW_PREFIX}-" \
        --prefix="${DEPENDS_PREFIX}" \
        no-shared no-tests no-docs
    make -j"${BUILD_JOBS}"
    make install_sw
    rm -rf /tmp/openssl-win-build
    cd "${REPO_ROOT}"
fi

# Build liboqs for Windows cross-compile
# Always rebuild if the submodule version changed (stale cache from prior liboqs version)
LIBOQS_SRC="${BCORE_DIR}/src/external/liboqs"
LIBOQS_NEED_BUILD=false
if [[ -n "${DEPENDS_PREFIX:-}" ]]; then
    if [[ ! -f "${DEPENDS_PREFIX}/lib/liboqs.a" ]]; then
        LIBOQS_NEED_BUILD=true
    elif [[ -f "${LIBOQS_SRC}/CMakeLists.txt" ]]; then
        # Extract source version and compare with installed
        SRC_VER=$(grep 'set(OQS_VERSION_MINOR' "${LIBOQS_SRC}/CMakeLists.txt" | grep -o '[0-9]*' || echo "0")
        INSTALLED_MARKER="${DEPENDS_PREFIX}/lib/.liboqs_minor_version"
        INSTALLED_VER=$(cat "$INSTALLED_MARKER" 2>/dev/null || echo "0")
        if [[ "$SRC_VER" != "$INSTALLED_VER" ]]; then
            log_info "liboqs version mismatch (installed minor=$INSTALLED_VER, source minor=$SRC_VER) — rebuilding"
            rm -f "${DEPENDS_PREFIX}/lib/liboqs.a"
            LIBOQS_NEED_BUILD=true
        fi
    fi
fi
if [[ "${LIBOQS_NEED_BUILD}" == "true" ]]; then
    log_info "Building liboqs for Windows cross-compile..."
    LIBOQS_SRC="${BCORE_DIR}/src/external/liboqs"
    rm -rf /tmp/liboqs-win-build
    mkdir -p /tmp/liboqs-win-build
    # Create mingw toolchain for liboqs
    cat > /tmp/liboqs-win-toolchain.cmake <<TCEOF
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER ${MINGW_PREFIX}-gcc-posix)
set(CMAKE_CXX_COMPILER ${MINGW_PREFIX}-g++-posix)
set(CMAKE_FIND_ROOT_PATH /usr/${MINGW_PREFIX} ${DEPENDS_PREFIX})
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
TCEOF
    cmake -S "${LIBOQS_SRC}" -B /tmp/liboqs-win-build \
        --toolchain /tmp/liboqs-win-toolchain.cmake \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${DEPENDS_PREFIX}" \
        -DBUILD_SHARED_LIBS=OFF \
        -DOQS_USE_OPENSSL=OFF \
        -DOQS_BUILD_ONLY_LIB=ON \
        -DOQS_MINIMAL_BUILD="SIG_ml_dsa_44;SIG_ml_dsa_65;SIG_ml_dsa_87" \
        -DOQS_DIST_BUILD=ON
    cmake --build /tmp/liboqs-win-build -j"${BUILD_JOBS}"
    cmake --install /tmp/liboqs-win-build
    # Write version marker so stale cache is detected on next run
    SRC_VER=$(grep 'set(OQS_VERSION_MINOR' "${LIBOQS_SRC}/CMakeLists.txt" | grep -o '[0-9]*' || echo "0")
    echo "$SRC_VER" > "${DEPENDS_PREFIX}/lib/.liboqs_minor_version"
    rm -rf /tmp/liboqs-win-build /tmp/liboqs-win-toolchain.cmake
    cd "${REPO_ROOT}"
fi

# Build secp256k1-zkp for Windows cross-compile
if [[ -n "${DEPENDS_PREFIX:-}" && ! -f "${DEPENDS_PREFIX}/lib/libsecp256k1.a" ]]; then
    log_info "Building secp256k1-zkp for Windows cross-compile..."
    SECP_SRC="${BCORE_DIR}/src/external/secp256k1-zkp"
    cd "${SECP_SRC}"
    ./autogen.sh
    ./configure \
        --host="${MINGW_PREFIX}" \
        --prefix="${DEPENDS_PREFIX}" \
        --disable-shared --enable-static \
        --disable-benchmark --disable-tests \
        --enable-experimental \
        --enable-module-ecdh \
        --enable-module-extrakeys \
        --enable-module-schnorrsig \
        --enable-module-musig \
        --enable-module-ellswift \
        --enable-module-ecdsa-adaptor \
        --enable-module-recovery \
        CC="${MINGW_PREFIX}-gcc-posix"
    make -j"${BUILD_JOBS}"
    make install
    cd "${REPO_ROOT}"
fi

# Point CMake to depends prefix for blst, OpenSSL, GMP, liboqs, secp256k1
if [[ -n "${DEPENDS_PREFIX:-}" ]]; then
    CMAKE_ARGS+=(
        -DBLST_LIBRARY="${DEPENDS_PREFIX}/lib/libblst.a"
        -DBLST_INCLUDE_DIR="${DEPENDS_PREFIX}/include"
        -DGMP_INCLUDE_DIR="${DEPENDS_PREFIX}/include"
        -DGMP_LIBRARY="${DEPENDS_PREFIX}/lib/libgmp.a"
        -DGMPXX_LIBRARY="${DEPENDS_PREFIX}/lib/libgmpxx.a"
        -DOPENSSL_ROOT_DIR="${DEPENDS_PREFIX}"
        -DSECP256K1_INCLUDE_DIR="${DEPENDS_PREFIX}/include"
        -DSECP256K1_LIBRARY="${DEPENDS_PREFIX}/lib/libsecp256k1.a"
    )
fi

cmake -S "${BCORE_DIR}" -B "${BUILD_DIR}" "${CMAKE_ARGS[@]}"

if [[ "${CONFIGURE_ONLY}" == true ]]; then
    log_info "Configure-only flag set; skipping cosign-bridge and build."
    exit 0
fi

# =============================================================================
# Step 3: Build cosign-bridge for Windows
# =============================================================================

log_info "Building cosign-bridge for Windows..."

# Ensure Rust target is installed
rustup target add "${RUST_TARGET}" 2>/dev/null || true

# Configure Rust for mingw — ensure CARGO_HOME is writable
export CARGO_HOME="${HOME}/.cargo"
mkdir -p "${CARGO_HOME}"
cat >> "${CARGO_HOME}/config.toml" << EOF 2>/dev/null || true
[target.${RUST_TARGET}]
linker = "${MINGW_PREFIX}-gcc"
EOF

cargo build --release \
    --manifest-path "${COSIGN_DIR}/Cargo.toml" \
    -j "${BUILD_JOBS}" \
    --target "${RUST_TARGET}"

cp "${COSIGN_DIR}/target/${RUST_TARGET}/release/cosign-bridge.exe" "${INSTALL_DIR}/"

# =============================================================================
# Step 4: Build Windows executables
# =============================================================================

log_info "Building Windows executables..."
cmake --build "${BUILD_DIR}" --target bitcoin-qt bitcoin-cli -j"${BUILD_JOBS}"

# =============================================================================
# Step 5: Assemble distribution
# =============================================================================

log_info "Assembling Windows distribution..."

# Copy executables
cp "${BUILD_DIR}/bin/bitcoin-qt.exe" "${INSTALL_DIR}/${APP_NAME}.exe"
cp "${BUILD_DIR}/bin/bitcoin-cli.exe" "${INSTALL_DIR}/"

# Copy Qt DLLs (if built with depends)
if [[ -n "${DEPENDS_PREFIX:-}" ]]; then
    cp "${DEPENDS_PREFIX}/lib/"*.dll "${INSTALL_DIR}/" 2>/dev/null || true
    mkdir -p "${INSTALL_DIR}/platforms"
    cp "${DEPENDS_PREFIX}/plugins/platforms/qwindows.dll" "${INSTALL_DIR}/platforms/" 2>/dev/null || true
fi

# Copy other runtime DLLs
for dll in libgcc_s_seh-1.dll libstdc++-6.dll libwinpthread-1.dll; do
    DLL_PATH="$("${MINGW_PREFIX}-gcc" -print-file-name="${dll}" 2>/dev/null || true)"
    if [[ -f "${DLL_PATH}" ]]; then
        cp "${DLL_PATH}" "${INSTALL_DIR}/"
    fi
done

# Create default config
mkdir -p "${INSTALL_DIR}/defaults"
if [[ -f "${SCRIPT_DIR}/../common/validator-config.json" ]]; then
    cp "${SCRIPT_DIR}/../common/validator-config.json" "${INSTALL_DIR}/defaults/"
fi
if [[ -f "${SCRIPT_DIR}/../common/default-bitcoin.conf" ]]; then
    cp "${SCRIPT_DIR}/../common/default-bitcoin.conf" "${INSTALL_DIR}/defaults/bitcoin.conf"
else
    log_warn "default-bitcoin.conf not found, Windows build will lack default config"
fi

# Icon already copied at script start (fail-fast)

# Bundle Tor binary for onion peer discovery
TOR_BINARY="${TOR_BINARY:-}"
TOR_VERSION="${TOR_VERSION:-15.0.18}"
if [[ -z "${TOR_BINARY}" ]]; then
    # Try to find a pre-existing tor.exe in depends
    for candidate in "${DEPENDS_PREFIX:-}/bin/tor.exe"; do
        if [[ -f "${candidate}" ]]; then
            TOR_BINARY="${candidate}"
            break
        fi
    done
fi
# Download Tor Expert Bundle for Windows if not found locally.
# Tor Project prunes old releases from dist.torproject.org, so a hardcoded
# pin will eventually 404 — fall back to the latest stable on the index.
fetch_tor_bundle() {
    local version="$1"
    local archive="tor-expert-bundle-windows-x86_64-${version}.tar.gz"
    local url="https://dist.torproject.org/torbrowser/${version}/${archive}"
    log_info "  Trying ${url}"
    curl -fsSL "${url}" -o "${TOR_BUNDLE_DIR}/${archive}"
}

discover_latest_tor_version() {
    # Match X.Y.Z directory entries; skip alpha/beta (e.g. 15.0a3).
    curl -fsSL "https://dist.torproject.org/torbrowser/" 2>/dev/null \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+/' \
        | tr -d '/' \
        | sort -V -u \
        | tail -1
}

if [[ -z "${TOR_BINARY}" || ! -f "${TOR_BINARY}" ]]; then
    log_info "Downloading Tor Expert Bundle ${TOR_VERSION} for Windows..."
    TOR_BUNDLE_DIR="${STAGING_DIR}/tor-bundle"
    mkdir -p "${TOR_BUNDLE_DIR}"

    if ! fetch_tor_bundle "${TOR_VERSION}"; then
        log_warn "Pinned Tor version ${TOR_VERSION} unavailable; discovering latest..."
        LATEST_TOR="$(discover_latest_tor_version || true)"
        if [[ -n "${LATEST_TOR}" && "${LATEST_TOR}" != "${TOR_VERSION}" ]]; then
            log_info "Falling back to Tor Expert Bundle ${LATEST_TOR}"
            if fetch_tor_bundle "${LATEST_TOR}"; then
                TOR_VERSION="${LATEST_TOR}"
            fi
        fi
    fi

    TOR_ARCHIVE="tor-expert-bundle-windows-x86_64-${TOR_VERSION}.tar.gz"
    if [[ -f "${TOR_BUNDLE_DIR}/${TOR_ARCHIVE}" ]]; then
        tar -xzf "${TOR_BUNDLE_DIR}/${TOR_ARCHIVE}" -C "${TOR_BUNDLE_DIR}"
        TOR_BINARY="$(find "${TOR_BUNDLE_DIR}" -name 'tor.exe' -type f | head -1)"
    else
        log_warn "Failed to download Tor Expert Bundle (pinned=${TOR_VERSION})"
    fi
fi
if [[ -n "${TOR_BINARY}" && -f "${TOR_BINARY}" ]]; then
    log_info "Bundling Tor binary from ${TOR_BINARY}..."
    cp "${TOR_BINARY}" "${INSTALL_DIR}/tor.exe"
    # Copy any DLLs alongside tor.exe (libevent, libssl, libcrypto, zlib)
    TOR_PARENT="$(dirname "${TOR_BINARY}")"
    for dll in "${TOR_PARENT}"/*.dll; do
        if [[ -f "${dll}" ]]; then
            log_info "  Bundling Tor dependency: $(basename "${dll}")"
            cp "${dll}" "${INSTALL_DIR}/"
        fi
    done
else
    log_error "Tor binary not found. Desktop wallet will not be able to connect to Tor-only networks."
    log_error "Set TOR_BINARY=/path/to/tor.exe or TOR_VERSION to download it."
    exit 1
fi

# =============================================================================
# Step 5: Create NSIS installer
# =============================================================================

log_info "Creating NSIS installer..."

if ! command -v makensis &>/dev/null; then
    log_warn "NSIS not found. Skipping installer creation."
    log_warn "Install with: apt install nsis OR brew install nsis"
else
    # Generate NSIS script
    NSIS_SCRIPT="${STAGING_DIR}/installer.nsi"
    cat > "${NSIS_SCRIPT}" << EOF
; TensorCash Windows Installer
!include "MUI2.nsh"

Name "${APP_NAME}"
OutFile "../${APP_NAME}-${APP_VERSION}-win-${TARGET_ARCH}-setup.exe"
InstallDir "\$PROGRAMFILES64\\${APP_NAME}"
RequestExecutionLevel admin

!define MUI_ICON "${APP_NAME}/TensorCash.ico"
!define MUI_UNICON "${APP_NAME}/TensorCash.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${BCORE_DIR}/COPYING"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SetOutPath "\$INSTDIR"
    File /r "${APP_NAME}\\*.*"

    ; Create uninstaller
    WriteUninstaller "\$INSTDIR\\uninstall.exe"

    ; Start menu shortcuts
    CreateDirectory "\$SMPROGRAMS\\${APP_NAME}"
    CreateShortcut "\$SMPROGRAMS\\${APP_NAME}\\${APP_NAME}.lnk" "\$INSTDIR\\${APP_NAME}.exe"
    CreateShortcut "\$SMPROGRAMS\\${APP_NAME}\\Uninstall.lnk" "\$INSTDIR\\uninstall.exe"

    ; Desktop shortcut
    CreateShortcut "\$DESKTOP\\${APP_NAME}.lnk" "\$INSTDIR\\${APP_NAME}.exe"

    ; Registry entries for uninstall
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}" "UninstallString" "\$INSTDIR\\uninstall.exe"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}" "Publisher" "TensorCash"
SectionEnd

Section "Uninstall"
    ; Remove files
    RMDir /r "\$INSTDIR"

    ; Remove shortcuts
    RMDir /r "\$SMPROGRAMS\\${APP_NAME}"
    Delete "\$DESKTOP\\${APP_NAME}.lnk"

    ; Remove registry entries
    DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}"
SectionEnd
EOF

    makensis "${NSIS_SCRIPT}"
fi

# Also create a portable ZIP
log_info "Creating portable ZIP..."
(cd "${STAGING_DIR}" && zip -r "../${APP_NAME}-${APP_VERSION}-win-${TARGET_ARCH}-portable.zip" "${APP_NAME}")

# =============================================================================
# Done
# =============================================================================

log_info "Build complete!"
log_info "Portable: ${OUTPUT_DIR}/${APP_NAME}-${APP_VERSION}-win-${TARGET_ARCH}-portable.zip"
if [[ -f "${OUTPUT_DIR}/${APP_NAME}-${APP_VERSION}-win-${TARGET_ARCH}-setup.exe" ]]; then
    log_info "Installer: ${OUTPUT_DIR}/${APP_NAME}-${APP_VERSION}-win-${TARGET_ARCH}-setup.exe"
fi
log_info ""
log_info "Next steps:"
log_info "  1. Test in Windows VM or Wine"
log_info "  2. Sign with Authenticode: ./sign-windows.sh"
