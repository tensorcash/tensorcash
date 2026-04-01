#!/usr/bin/env bash
# Bundle miner-proxy as a standalone binary using PyInstaller
# Output: miner-proxy executable with all dependencies included

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-${SCRIPT_DIR}/../build}"
OUTPUT_DIR="${BUILD_DIR}/output"

MINER_API_DIR="${PROJECT_ROOT}/services/miner-api"
SHARED_UTILS_DIR="${PROJECT_ROOT}/shared-utils"

echo "=== Bundling miner-proxy with PyInstaller ==="

mkdir -p "${BUILD_DIR}/miner-proxy-bundle" "${OUTPUT_DIR}"
cd "${BUILD_DIR}/miner-proxy-bundle"

# -----------------------------------------------------------------------------
# Create virtual environment (requires Python 3.10+ for mcp package)
# -----------------------------------------------------------------------------
echo "=== Creating virtual environment ==="

# Find Python 3.10+
PYTHON_CMD=""
for py in python3.12 python3.11 python3.10; do
    if command -v $py &> /dev/null; then
        PYTHON_CMD=$py
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python 3.10+ required. Found:"
    python3 --version
    exit 1
fi

echo "Using $PYTHON_CMD: $($PYTHON_CMD --version)"
$PYTHON_CMD -m venv venv
source venv/bin/activate

pip install --upgrade pip wheel setuptools
pip install pyinstaller

# -----------------------------------------------------------------------------
# Install miner-proxy dependencies
# -----------------------------------------------------------------------------
echo "=== Installing miner-proxy dependencies ==="
pip install -r "${MINER_API_DIR}/proxy_requirements.txt"

# ChiaVDF - build from source for macOS
echo "=== Building ChiaVDF from source ==="
PYTHON_CMD="$(which python)" "${SCRIPT_DIR}/build-chiavdf.sh" || {
    echo "ERROR: ChiaVDF build failed"
    exit 1
}

# -----------------------------------------------------------------------------
# Prepare source tree for bundling
# -----------------------------------------------------------------------------
echo "=== Preparing source tree ==="

BUNDLE_SRC="${BUILD_DIR}/miner-proxy-bundle/src"
rm -rf "${BUNDLE_SRC}"
mkdir -p "${BUNDLE_SRC}"

# Copy miner-proxy source
cp -r "${MINER_API_DIR}/src/"* "${BUNDLE_SRC}/"

# Copy shared utils
mkdir -p "${BUNDLE_SRC}/utils"
cp "${SHARED_UTILS_DIR}/pow-utils/pow_utils.py" "${BUNDLE_SRC}/utils/"
cp "${SHARED_UTILS_DIR}/pow-utils/uint256_arithmetics.py" "${BUNDLE_SRC}/utils/"

mkdir -p "${BUNDLE_SRC}/config"
cp "${SHARED_UTILS_DIR}/config/constants.py" "${BUNDLE_SRC}/config/"

# Generate FlatBuffer Python files
echo "=== Generating FlatBuffer files ==="
cd "${BUNDLE_SRC}"

# Use flatc if available, otherwise use flatbuffers Python package
if command -v flatc &> /dev/null; then
    flatc --python "${SHARED_UTILS_DIR}/fb-schemas/proof.fbs"
    flatc --python "${SHARED_UTILS_DIR}/fb-schemas/validation.fbs"
    flatc --python "${SHARED_UTILS_DIR}/fb-schemas/blockheader.fbs"
else
    # flatc might be in our deps directory
    FLATC="${BUILD_DIR}/deps/bin/flatc"
    if [ -f "${FLATC}" ]; then
        "${FLATC}" --python "${SHARED_UTILS_DIR}/fb-schemas/proof.fbs"
        "${FLATC}" --python "${SHARED_UTILS_DIR}/fb-schemas/validation.fbs"
        "${FLATC}" --python "${SHARED_UTILS_DIR}/fb-schemas/blockheader.fbs"
    else
        echo "ERROR: flatc not found. Run build-llama-static.sh first or install flatbuffers."
        exit 1
    fi
fi

cd "${BUILD_DIR}/miner-proxy-bundle"

# -----------------------------------------------------------------------------
# Create PyInstaller spec
# -----------------------------------------------------------------------------
echo "=== Creating PyInstaller spec ==="

cat > miner-proxy.spec << 'EOF'
# -*- mode: python ; coding: utf-8 -*-
import os
import sys

block_cipher = None

# Collect all source files
src_path = os.path.join(SPECPATH, 'src')

a = Analysis(
    [os.path.join(src_path, 'main.py')],
    pathex=[src_path],
    binaries=[],
    datas=[
        (os.path.join(src_path, 'components'), 'components'),
        (os.path.join(src_path, 'config'), 'config'),
        (os.path.join(src_path, 'utils'), 'utils'),
        (os.path.join(src_path, 'proof'), 'proof'),
        (os.path.join(src_path, 'defaults'), 'defaults'),
    ],
    hiddenimports=[
        'aiohttp',
        'websockets',
        'zmq',
        'httpx',
        'numpy',
        'flatbuffers',
        'chiavdf',
        'mcp',
        'uvicorn',
        'starlette',
        'components.vdf_service',
        'components.zmq_listener',
        'components.proxy',
        'components.proxy_with_priority',
        'components.request_priority_manager',
        'components.proof_cache',
        'components.proof_collector',
        'components.model_synch',
        'components.context',
        'components.constants',
        'worker_client',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='miner-proxy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=os.environ.get('CODESIGN_IDENTITY', ''),
    entitlements_file=None,
)
EOF

# -----------------------------------------------------------------------------
# Build with PyInstaller
# -----------------------------------------------------------------------------
echo "=== Running PyInstaller ==="

pyinstaller \
    --clean \
    --noconfirm \
    --distpath "${OUTPUT_DIR}" \
    --workpath "${BUILD_DIR}/miner-proxy-bundle/build" \
    miner-proxy.spec

# -----------------------------------------------------------------------------
# Sign binary
# -----------------------------------------------------------------------------
echo "=== Signing miner-proxy ==="

if [ -f "${OUTPUT_DIR}/miner-proxy" ]; then
    # Clear quarantine/provenance attributes
    xattr -cr "${OUTPUT_DIR}/miner-proxy" 2>/dev/null || true

    # Sign with identity if available, otherwise ad-hoc
    SIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
    codesign -s "${SIGN_IDENTITY}" --force --timestamp "${OUTPUT_DIR}/miner-proxy" 2>/dev/null || \
    codesign -s - --force "${OUTPUT_DIR}/miner-proxy"

    codesign -v "${OUTPUT_DIR}/miner-proxy" && echo "Signature verified" || echo "Signature verification failed"
fi

# -----------------------------------------------------------------------------
# Verify output
# -----------------------------------------------------------------------------
echo "=== Verifying output ==="

if [ -f "${OUTPUT_DIR}/miner-proxy" ]; then
    echo "miner-proxy built successfully"
    ls -la "${OUTPUT_DIR}/miner-proxy"
    # IMPORTANT: do not execute the binary here; execution can start long-lived services.
    file "${OUTPUT_DIR}/miner-proxy" || true
else
    echo "ERROR: miner-proxy not found in output directory"
    exit 1
fi

deactivate

echo "=== Bundle complete ==="
echo "Output: ${OUTPUT_DIR}/miner-proxy"
