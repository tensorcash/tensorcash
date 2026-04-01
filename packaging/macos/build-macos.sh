#!/usr/bin/env bash
# =============================================================================
# TensorCash macOS Build Script
# =============================================================================
#
# Builds a self-contained TensorCash.app bundle for macOS.
#
# Usage:
#   ./build-macos.sh [options]
#
# Options:
#   --release       Build in release mode (default)
#   --debug         Build in debug mode
#   --arch ARCH     Target architecture: x86_64, arm64, or universal (default: native)
#   --qt-dir DIR    Path to Qt installation (default: auto-detect via brew)
#   --output DIR    Output directory (default: ./dist)
#   --skip-deps     Skip dependency builds (use existing)
#   --clean         Clean build directories first
#
# Requirements:
#   - Xcode Command Line Tools
#   - Homebrew with: qt@6 zeromq boost gmp flint sqlite cmake
#   - Rust toolchain
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BCORE_DIR="${REPO_ROOT}/services/core-node/bcore"
COSIGN_DIR="${REPO_ROOT}/services/core-node/cosign-bridge"

# Defaults
BUILD_TYPE="Release"
TARGET_ARCH="$(uname -m)"
QT_DIR=""
OUTPUT_DIR="${SCRIPT_DIR}/dist"
SKIP_DEPS=false
CLEAN=false

# App metadata
DEFAULT_CHAIN_TYPE="${DEFAULT_CHAIN_TYPE:-tensor}"
APP_NAME="TensorCash"
APP_VERSION="$(grep 'CLIENT_VERSION_MAJOR' "${BCORE_DIR}/CMakeLists.txt" | head -1 | grep -oE '[0-9]+')"
APP_VERSION+=".$(grep 'CLIENT_VERSION_MINOR' "${BCORE_DIR}/CMakeLists.txt" | head -1 | grep -oE '[0-9]+')"
APP_VERSION+=".$(grep 'CLIENT_VERSION_BUILD' "${BCORE_DIR}/CMakeLists.txt" | head -1 | grep -oE '[0-9]+')"
BUNDLE_ID="io.tensorcash.wallet"

case "${DEFAULT_CHAIN_TYPE}" in
    tensor-test)
        APP_NAME="TensorCash-Testnet"
        BUNDLE_ID="io.tensorcash.wallet.testnet"
        ;;
    tensor)
        ;;
    *)
        echo "Unsupported DEFAULT_CHAIN_TYPE: ${DEFAULT_CHAIN_TYPE}" >&2
        exit 1
        ;;
esac

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --release) BUILD_TYPE="Release"; shift ;;
        --debug) BUILD_TYPE="Debug"; shift ;;
        --arch) TARGET_ARCH="$2"; shift 2 ;;
        --qt-dir) QT_DIR="$2"; shift 2 ;;
        --output) OUTPUT_DIR="$2"; shift 2 ;;
        --skip-deps) SKIP_DEPS=true; shift ;;
        --clean) CLEAN=true; shift ;;
        -h|--help)
            head -30 "$0" | tail -25
            exit 0
            ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Detect Qt
if [[ -z "${QT_DIR}" ]]; then
    if command -v brew &>/dev/null; then
        QT_DIR="$(brew --prefix qt@6 2>/dev/null || echo "")"
    fi
    if [[ -z "${QT_DIR}" || ! -d "${QT_DIR}" ]]; then
        log_error "Qt6 not found. Install with: brew install qt@6"
        exit 1
    fi
fi
log_info "Using Qt from: ${QT_DIR}"

# Setup build directories
BUILD_DIR="${BCORE_DIR}/build-macos-${TARGET_ARCH}"
STAGING_DIR="${OUTPUT_DIR}/staging"
APP_BUNDLE="${STAGING_DIR}/${APP_NAME}.app"

if [[ "${CLEAN}" == true ]]; then
    log_info "Cleaning build directories..."
    rm -rf "${BUILD_DIR}" "${STAGING_DIR}"
fi

mkdir -p "${BUILD_DIR}" "${STAGING_DIR}" "${OUTPUT_DIR}"

# =============================================================================
# Step 1: Build external dependencies
# =============================================================================

if [[ "${SKIP_DEPS}" != true ]]; then
    log_info "Building external dependencies..."

    # Build liboqs
    LIBOQS_DIR="${BCORE_DIR}/src/external/liboqs"
    if [[ -d "${LIBOQS_DIR}" ]]; then
        log_info "Building liboqs..."
        cmake -S "${LIBOQS_DIR}" -B "${LIBOQS_DIR}/build-macos" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_OSX_ARCHITECTURES="${TARGET_ARCH}" \
            -DBUILD_SHARED_LIBS=OFF \
            -DOQS_USE_OPENSSL=OFF \
            -DOQS_BUILD_ONLY_LIB=ON \
            -DOQS_MINIMAL_BUILD="SIG_ml_dsa_44;SIG_ml_dsa_65;SIG_ml_dsa_87"
        cmake --build "${LIBOQS_DIR}/build-macos" -j"$(sysctl -n hw.ncpu)"
    fi

    # Build secp256k1-zkp
    SECP_DIR="${BCORE_DIR}/src/external/secp256k1-zkp"
    if [[ -d "${SECP_DIR}" && ! -f "${SECP_DIR}/.libs/libsecp256k1.a" ]]; then
        log_info "Building secp256k1-zkp..."
        (cd "${SECP_DIR}" && \
            ./autogen.sh && \
            ./configure --disable-shared --enable-static \
                --enable-experimental \
                --enable-module-ecdh \
                --enable-module-extrakeys \
                --enable-module-schnorrsig \
                --enable-module-musig \
                --enable-module-ellswift \
                --enable-module-ecdsa-adaptor \
                --enable-module-recovery && \
            make -j"$(sysctl -n hw.ncpu)")
    fi
fi

# =============================================================================
# Step 2: Build cosign-bridge (Rust)
# =============================================================================

log_info "Building cosign-bridge..."
COSIGN_TARGET=""
case "${TARGET_ARCH}" in
    x86_64) COSIGN_TARGET="x86_64-apple-darwin" ;;
    arm64) COSIGN_TARGET="aarch64-apple-darwin" ;;
    universal)
        # Build for both architectures
        cargo build --release --manifest-path "${COSIGN_DIR}/Cargo.toml" --target x86_64-apple-darwin
        cargo build --release --manifest-path "${COSIGN_DIR}/Cargo.toml" --target aarch64-apple-darwin
        mkdir -p "${BUILD_DIR}/bin"
        lipo -create \
            "${COSIGN_DIR}/target/x86_64-apple-darwin/release/cosign-bridge" \
            "${COSIGN_DIR}/target/aarch64-apple-darwin/release/cosign-bridge" \
            -output "${BUILD_DIR}/bin/cosign-bridge"
        COSIGN_TARGET="done"
        ;;
esac

if [[ "${COSIGN_TARGET}" != "done" ]]; then
    if [[ -n "${COSIGN_TARGET}" ]]; then
        cargo build --release --manifest-path "${COSIGN_DIR}/Cargo.toml" --target "${COSIGN_TARGET}"
        mkdir -p "${BUILD_DIR}/bin"
        cp "${COSIGN_DIR}/target/${COSIGN_TARGET}/release/cosign-bridge" "${BUILD_DIR}/bin/"
    else
        cargo build --release --manifest-path "${COSIGN_DIR}/Cargo.toml"
        mkdir -p "${BUILD_DIR}/bin"
        cp "${COSIGN_DIR}/target/release/cosign-bridge" "${BUILD_DIR}/bin/"
    fi
fi

# =============================================================================
# Step 3: Build bitcoin-qt
# =============================================================================

log_info "Configuring bitcoin-qt build..."

CMAKE_ARGS=(
    -DCMAKE_BUILD_TYPE="${BUILD_TYPE}"
    -DCMAKE_PREFIX_PATH="${QT_DIR}"
    -DCMAKE_OSX_ARCHITECTURES="${TARGET_ARCH}"
    -DDEFAULT_CHAIN_TYPE="${DEFAULT_CHAIN_TYPE}"
    -DBUILD_DAEMON=OFF
    -DBUILD_CLI=ON
    -DBUILD_GUI=ON
    -DBUILD_TESTS=OFF
    -DBUILD_BENCH=OFF
    -DWITH_ZMQ=ON
    -DENABLE_WALLET=ON
    -DWITH_QRENCODE=ON
    -DREDUCE_EXPORTS=ON
)

# Generate FlatBuffers headers first
log_info "Generating FlatBuffers headers..."
flatc --cpp -o "${BCORE_DIR}/src/rpc" \
    "${REPO_ROOT}/shared-utils/fb-schemas/proof.fbs" \
    "${REPO_ROOT}/shared-utils/fb-schemas/blockheader.fbs" \
    "${REPO_ROOT}/shared-utils/fb-schemas/validation.fbs"

cmake -S "${BCORE_DIR}" -B "${BUILD_DIR}" "${CMAKE_ARGS[@]}"

log_info "Building bitcoin-qt..."
cmake --build "${BUILD_DIR}" --target bitcoin-qt bitcoin-cli -j"$(sysctl -n hw.ncpu)"

# =============================================================================
# Step 4: Create app bundle
# =============================================================================

log_info "Creating app bundle..."

# Create bundle structure
mkdir -p "${APP_BUNDLE}/Contents/"{MacOS,Frameworks,Resources,PlugIns}

# Copy main executable
cp "${BUILD_DIR}/bin/bitcoin-qt" "${APP_BUNDLE}/Contents/MacOS/${APP_NAME}"

# Copy helper binaries
cp "${BUILD_DIR}/bin/cosign-bridge" "${APP_BUNDLE}/Contents/MacOS/"
cp "${BUILD_DIR}/bin/bitcoin-cli" "${APP_BUNDLE}/Contents/MacOS/"

# Generate Info.plist
cat > "${APP_BUNDLE}/Contents/Info.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIconFile</key>
    <string>TensorCash</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>${APP_VERSION}</string>
    <key>CFBundleVersion</key>
    <string>${APP_VERSION}</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key>
    <true/>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeExtensions</key>
            <array>
                <string>tensorcash</string>
            </array>
            <key>CFBundleTypeName</key>
            <string>TensorCash URI</string>
            <key>CFBundleTypeRole</key>
            <string>Viewer</string>
        </dict>
    </array>
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>${BUNDLE_ID}</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>tensorcash</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
EOF

# Copy icon if exists
if [[ -f "${SCRIPT_DIR}/TensorCash.icns" ]]; then
    cp "${SCRIPT_DIR}/TensorCash.icns" "${APP_BUNDLE}/Contents/Resources/"
fi

# Copy default configuration
mkdir -p "${APP_BUNDLE}/Contents/Resources/defaults"
if [[ -f "${SCRIPT_DIR}/../common/validator-config.json" ]]; then
    cp "${SCRIPT_DIR}/../common/validator-config.json" "${APP_BUNDLE}/Contents/Resources/defaults/"
fi
if [[ -f "${SCRIPT_DIR}/../common/default-bitcoin.conf" ]]; then
    cp "${SCRIPT_DIR}/../common/default-bitcoin.conf" "${APP_BUNDLE}/Contents/Resources/defaults/bitcoin.conf"
fi

# Bundle Tor binary and its dylib dependencies for onion peer discovery
TOR_BINARY="${TOR_BINARY:-$(command -v tor 2>/dev/null || true)}"
if [[ -n "${TOR_BINARY}" && -x "${TOR_BINARY}" ]]; then
    log_info "Bundling Tor binary from ${TOR_BINARY}..."
    cp "${TOR_BINARY}" "${APP_BUNDLE}/Contents/MacOS/tor"
    chmod +x "${APP_BUNDLE}/Contents/MacOS/tor"

    # Copy all non-system dylib dependencies and rewrite rpaths so tor
    # finds them next to itself inside Contents/Frameworks/
    FRAMEWORKS_DIR="${APP_BUNDLE}/Contents/Frameworks"
    mkdir -p "${FRAMEWORKS_DIR}"
    for dylib in $(otool -L "${APP_BUNDLE}/Contents/MacOS/tor" | awk 'NR>1{print $1}' | grep -v '/usr/lib\|/System' | grep -v '^@'); do
        dylib_name="$(basename "${dylib}")"
        if [[ ! -f "${FRAMEWORKS_DIR}/${dylib_name}" ]]; then
            log_info "  Bundling Tor dependency: ${dylib_name}"
            cp "${dylib}" "${FRAMEWORKS_DIR}/${dylib_name}"
            chmod 644 "${FRAMEWORKS_DIR}/${dylib_name}"
        fi
        install_name_tool -change "${dylib}" "@executable_path/../Frameworks/${dylib_name}" \
            "${APP_BUNDLE}/Contents/MacOS/tor" 2>/dev/null || true
    done
else
    log_error "Tor binary not found. Desktop wallet will not be able to connect to Tor-only networks."
    log_error "Install Tor (brew install tor) or set TOR_BINARY=/path/to/tor to bundle it."
    exit 1
fi

# =============================================================================
# Step 5: Deploy Qt frameworks
# =============================================================================

log_info "Deploying Qt frameworks..."

# Use macdeployqt to copy Qt frameworks
"${QT_DIR}/bin/macdeployqt" "${APP_BUNDLE}" \
    -verbose=1 \
    -always-overwrite

# If custom macdeployqtplus exists in contrib, use it for better control
if [[ -x "${BCORE_DIR}/contrib/macdeploy/macdeployqtplus" ]]; then
    log_info "Running macdeployqtplus for additional fixes..."
    "${BCORE_DIR}/contrib/macdeploy/macdeployqtplus" "${APP_BUNDLE}" \
        -add-qt-tr da,de,es,hu,it,ja,ko,nl,pl,pt_BR,ru,sr,uk,zh_CN,zh_TW
fi

# =============================================================================
# Step 6: Fix library paths
# =============================================================================

log_info "Fixing library paths..."

# Fix rpath for cosign-bridge
install_name_tool -add_rpath "@executable_path/../Frameworks" \
    "${APP_BUNDLE}/Contents/MacOS/cosign-bridge" 2>/dev/null || true

# Ensure all dylibs point to bundle-relative paths
find "${APP_BUNDLE}/Contents/Frameworks" -name "*.dylib" -exec \
    install_name_tool -add_rpath "@loader_path/../Frameworks" {} \; 2>/dev/null || true

# Generic recursive sweep: find ALL non-system dylib references pointing
# to /opt/homebrew or /usr/local, copy them into Frameworks, and rewrite
# the load paths. Repeat until no new deps are discovered (transitive).
log_info "Running generic dependency sweep..."
FRAMEWORKS_DIR="${APP_BUNDLE}/Contents/Frameworks"
max_passes=5
pass=0
while [[ ${pass} -lt ${max_passes} ]]; do
    pass=$((pass + 1))
    found_new=false

    for f in "${APP_BUNDLE}/Contents/MacOS/"* "${FRAMEWORKS_DIR}/"*.dylib "${FRAMEWORKS_DIR}/"*.framework/Versions/A/*; do
        [[ -f "${f}" ]] || continue
        while IFS= read -r dep; do
            [[ -z "${dep}" ]] && continue
            dep_name="$(basename "${dep}")"
            # If this is a Qt framework reference (already bundled as .framework),
            # only rewrite the path — do NOT copy as a bare file.
            if [[ -d "${FRAMEWORKS_DIR}/${dep_name}.framework" ]]; then
                install_name_tool -change "${dep}" \
                    "@executable_path/../Frameworks/${dep_name}.framework/Versions/A/${dep_name}" "${f}" 2>/dev/null || true
                continue
            fi
            # Copy if not already in Frameworks
            if [[ ! -f "${FRAMEWORKS_DIR}/${dep_name}" ]]; then
                if [[ -f "${dep}" ]]; then
                    log_info "  [pass ${pass}] Bundling missing dependency: ${dep_name} (needed by $(basename "${f}"))"
                    cp "${dep}" "${FRAMEWORKS_DIR}/${dep_name}"
                    chmod 644 "${FRAMEWORKS_DIR}/${dep_name}"
                    install_name_tool -id "@executable_path/../Frameworks/${dep_name}" \
                        "${FRAMEWORKS_DIR}/${dep_name}" 2>/dev/null || true
                    found_new=true
                else
                    log_warn "dependency ${dep} referenced by $(basename "${f}") not found on disk"
                fi
            fi
            # Rewrite reference
            install_name_tool -change "${dep}" "@executable_path/../Frameworks/${dep_name}" "${f}" 2>/dev/null || true
        done < <(otool -L "${f}" 2>/dev/null | awk 'NR>1{print $1}' | grep -E '^(/opt/homebrew|/usr/local)/' | grep -v '^/usr/lib')
    done

    if [[ "${found_new}" == false ]]; then
        log_info "  No new dependencies found in pass ${pass}, sweep complete."
        break
    fi
    log_info "  Pass ${pass} found new dependencies, scanning again for transitive deps..."
done

# Fix install names (self-references) for any dylib still pointing to Homebrew
for lib in "${FRAMEWORKS_DIR}/"*.dylib; do
    [[ -f "${lib}" ]] || continue
    current_id=$(otool -D "${lib}" 2>/dev/null | tail -1)
    if echo "${current_id}" | grep -qE '^(/opt/homebrew|/usr/local)/'; then
        lib_name="$(basename "${lib}")"
        install_name_tool -id "@executable_path/../Frameworks/${lib_name}" "${lib}" 2>/dev/null || true
    fi
done

# Final verification: fail if ANY Homebrew references remain
log_info "Verifying no Homebrew references remain..."
sweep_fail=false
while IFS= read -r f; do
    if otool -L "${f}" 2>/dev/null | grep -q "not found"; then
        log_error "Missing dependency in $(basename "${f}")"
        otool -L "${f}" | grep "not found"
        sweep_fail=true
    fi
    homebrew_refs=$(otool -L "${f}" 2>/dev/null | awk 'NR>1{print $1}' | grep -E '^(/opt/homebrew|/usr/local)/' | grep -v '^/usr/lib' || true)
    if [[ -n "${homebrew_refs}" ]]; then
        log_error "Unrewritten Homebrew reference in $(basename "${f}"):"
        echo "${homebrew_refs}"
        sweep_fail=true
    fi
done < <(find "${APP_BUNDLE}/Contents" -type f \( -perm -111 -o -name "*.dylib" \))
if [[ "${sweep_fail}" == true ]]; then
    log_error "Dependency sweep failed — see errors above"
    exit 1
fi
log_info "All dependencies properly bundled."

# =============================================================================
# Step 7: Create DMG (unsigned)
# =============================================================================

log_info "Creating DMG..."

DMG_NAME="${APP_NAME}-${APP_VERSION}-macos-${TARGET_ARCH}.dmg"
DMG_PATH="${OUTPUT_DIR}/${DMG_NAME}"

# Remove old DMG if exists
rm -f "${DMG_PATH}"

# Create DMG
hdiutil create -volname "${APP_NAME}" \
    -srcfolder "${APP_BUNDLE}" \
    -ov -format UDZO \
    "${DMG_PATH}"

# =============================================================================
# Done
# =============================================================================

log_info "Build complete!"
log_info "App bundle: ${APP_BUNDLE}"
log_info "DMG: ${DMG_PATH}"
log_info ""
log_info "Next steps:"
log_info "  1. Test the app: open '${APP_BUNDLE}'"
log_info "  2. Sign and notarize: ./sign-and-notarize.sh '${DMG_PATH}'"
