#!/usr/bin/env bash
# Build TensorMiner.app from Swift source + bundled binaries
# This script creates the full .app bundle ready for signing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-${SCRIPT_DIR}/../build}"
APP_DIR="${BUILD_DIR}/TensorMiner.app"
SWIFT_SRC="${SCRIPT_DIR}/../TensorMiner"

# Codesign identity (from environment or default)
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:-}"
TEAM_ID="${TEAM_ID:-YOUR_APPLE_TEAM_ID}"

echo "=== Building TensorMiner.app ==="

mkdir -p "${BUILD_DIR}"

# -----------------------------------------------------------------------------
# Build Swift app
# -----------------------------------------------------------------------------
echo "=== Compiling Swift UI app ==="

# Create temporary Swift package for building
SWIFT_PKG="${BUILD_DIR}/swift-pkg"
rm -rf "${SWIFT_PKG}"
mkdir -p "${SWIFT_PKG}/Sources/TensorMiner"

# Copy source files
cp "${SWIFT_SRC}/"*.swift "${SWIFT_PKG}/Sources/TensorMiner/"

# Create Package.swift
cat > "${SWIFT_PKG}/Package.swift" << 'EOF'
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "TensorMiner",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "TensorMiner", targets: ["TensorMiner"])
    ],
    targets: [
        .executableTarget(
            name: "TensorMiner",
            path: "Sources/TensorMiner"
        )
    ]
)
EOF

# Build
cd "${SWIFT_PKG}"
swift build -c release --arch arm64

# -----------------------------------------------------------------------------
# Create .app bundle structure
# -----------------------------------------------------------------------------
echo "=== Creating app bundle ==="

rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}/Contents/MacOS"
mkdir -p "${APP_DIR}/Contents/Resources"

# Copy executable
cp "${SWIFT_PKG}/.build/release/TensorMiner" "${APP_DIR}/Contents/MacOS/"

# Copy Info.plist
cp "${SWIFT_SRC}/Info.plist" "${APP_DIR}/Contents/"

# Update Info.plist with actual values
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier io.tensorcash.TensorMiner" "${APP_DIR}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable TensorMiner" "${APP_DIR}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName TensorMiner" "${APP_DIR}/Contents/Info.plist"

# Create PkgInfo
echo -n "APPL????" > "${APP_DIR}/Contents/PkgInfo"

# -----------------------------------------------------------------------------
# Bundle dependencies
# -----------------------------------------------------------------------------
echo "=== Bundling dependencies ==="

OUTPUT_DIR="${BUILD_DIR}/output"

# Copy llama-server if built
if [ -f "${OUTPUT_DIR}/llama-server" ]; then
    cp "${OUTPUT_DIR}/llama-server" "${APP_DIR}/Contents/Resources/"
    chmod +x "${APP_DIR}/Contents/Resources/llama-server"
    echo "Bundled llama-server"
else
    echo "WARNING: llama-server not found at ${OUTPUT_DIR}/llama-server"
    echo "Run build-llama-static.sh first"
fi

# Bundle canonical chat templates next to llama-server.
#
# MinerService.swift:resolveChatTemplateFile() reads from
# Resources/chat-templates/ at runtime. When the loaded GGUF's embedded
# chat_template is missing or unparseable by llama.cpp's autoparser (common
# for community Q4 quants), the service passes --chat-template-file to
# llama-server with the matching canonical Jinja from this directory — that
# restores the lazy PEG grammar that constrains tool_call JSON output.
#
# Templates live in services/miner-api/chat-templates/ as the single source
# of truth shared with the Docker workers (llama.Dockerfile and
# simple-worker-cpu/Dockerfile copy the same directory).
CHAT_TEMPLATE_SRC="${PROJECT_ROOT}/services/miner-api/chat-templates"
if [ -d "${CHAT_TEMPLATE_SRC}" ]; then
    mkdir -p "${APP_DIR}/Contents/Resources/chat-templates"
    cp -R "${CHAT_TEMPLATE_SRC}/." "${APP_DIR}/Contents/Resources/chat-templates/"
    echo "Bundled chat-templates from ${CHAT_TEMPLATE_SRC}"
else
    echo "WARNING: chat-templates dir not found at ${CHAT_TEMPLATE_SRC}"
fi

# Copy llama runtime dylibs if present (e.g., OpenSSL at @executable_path/libs)
if [ -d "${OUTPUT_DIR}/libs" ]; then
    mkdir -p "${APP_DIR}/Contents/Resources/libs"
    cp -R "${OUTPUT_DIR}/libs/." "${APP_DIR}/Contents/Resources/libs/"
    find "${APP_DIR}/Contents/Resources/libs" -type f -name "*.dylib" -exec chmod +x {} \;
    echo "Bundled runtime dylibs from output/libs"
fi

# Copy miner-proxy if built
if [ -f "${OUTPUT_DIR}/miner-proxy" ]; then
    cp "${OUTPUT_DIR}/miner-proxy" "${APP_DIR}/Contents/Resources/"
    chmod +x "${APP_DIR}/Contents/Resources/miner-proxy"
    echo "Bundled miner-proxy"
else
    echo "WARNING: miner-proxy not found at ${OUTPUT_DIR}/miner-proxy"
    echo "Run bundle-miner-proxy.sh first"
fi

# -----------------------------------------------------------------------------
# Codesign
# -----------------------------------------------------------------------------
if [ -n "${CODESIGN_IDENTITY}" ]; then
    echo "=== Signing app ==="

    # Sign bundled dylibs first so embedded binaries can validate at runtime
    if [ -d "${APP_DIR}/Contents/Resources/libs" ]; then
        while IFS= read -r dylib; do
            codesign --force --options runtime \
                --sign "${CODESIGN_IDENTITY}" \
                "${dylib}"
            echo "Signed: $(basename "${dylib}")"
        done < <(find "${APP_DIR}/Contents/Resources/libs" -type f -name "*.dylib" | sort)
    fi

    # Sign embedded binaries first
    for binary in "${APP_DIR}/Contents/Resources/llama-server" "${APP_DIR}/Contents/Resources/miner-proxy"; do
        if [ -f "${binary}" ]; then
            codesign --force --options runtime \
                --sign "${CODESIGN_IDENTITY}" \
                --entitlements "${SWIFT_SRC}/TensorMiner.entitlements" \
                "${binary}"
            echo "Signed: $(basename "${binary}")"
        fi
    done

    # Sign the main app
    codesign --force --options runtime \
        --sign "${CODESIGN_IDENTITY}" \
        --entitlements "${SWIFT_SRC}/TensorMiner.entitlements" \
        "${APP_DIR}"

    echo "App signed successfully"

    # Verify
    codesign --verify --deep --strict "${APP_DIR}"
    echo "Signature verified"
else
    echo "WARNING: CODESIGN_IDENTITY not set, skipping signing"
fi

# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------
echo "=== Build complete ==="
echo "App bundle: ${APP_DIR}"
ls -la "${APP_DIR}/Contents/MacOS/"
ls -la "${APP_DIR}/Contents/Resources/" 2>/dev/null || true
