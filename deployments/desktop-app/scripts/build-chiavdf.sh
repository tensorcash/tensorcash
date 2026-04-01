#!/usr/bin/env bash
# Build chiavdf for macOS (ARM64/x86_64)
# Requires: cmake, gmp (brew install cmake gmp)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-${SCRIPT_DIR}/../build}"
CHIAVDF_DIR="${PROJECT_ROOT}/shared-utils/chiavdf"

echo "=== Building chiavdf for macOS ==="

# Check dependencies
if ! command -v cmake &> /dev/null; then
    echo "ERROR: cmake not found. Install with: brew install cmake"
    exit 1
fi

# Check for GMP - try Homebrew paths
GMP_PREFIX=""
if [ -d "/opt/homebrew/opt/gmp" ]; then
    GMP_PREFIX="/opt/homebrew/opt/gmp"
elif [ -d "/usr/local/opt/gmp" ]; then
    GMP_PREFIX="/usr/local/opt/gmp"
elif pkg-config --exists gmp 2>/dev/null; then
    GMP_PREFIX="$(pkg-config --variable=prefix gmp)"
fi

if [ -z "$GMP_PREFIX" ]; then
    echo "ERROR: GMP not found. Install with: brew install gmp"
    exit 1
fi

echo "Using GMP from: $GMP_PREFIX"

# Setup build directory
mkdir -p "${BUILD_DIR}/chiavdf-build"
cd "${BUILD_DIR}/chiavdf-build"

# Get Python info
PYTHON_CMD="${PYTHON_CMD:-python3}"
PYTHON_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_INCLUDE=$($PYTHON_CMD -c "import sysconfig; print(sysconfig.get_path('include'))")
PYTHON_EXT_SUFFIX=$($PYTHON_CMD -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")

echo "Python version: $PYTHON_VERSION"
echo "Python include: $PYTHON_INCLUDE"
echo "Extension suffix: $PYTHON_EXT_SUFFIX"

# Initialize git repo if needed (for setuptools_scm)
cd "${CHIAVDF_DIR}"
if [ ! -d ".git" ]; then
    git init
    git config user.email "build@local"
    git config user.name "Build"
    git add .
    git commit -m "Initial commit" 2>/dev/null || true
    git tag -a v1.0.0 -m "Version 1.0.0" 2>/dev/null || true
fi

cd "${BUILD_DIR}/chiavdf-build"

# Run CMake with GMP paths
cmake "${CHIAVDF_DIR}/src" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_PYTHON=ON \
    -DBUILD_CHIAVDFC=OFF \
    -DGMP_INCLUDE_DIR="${GMP_PREFIX}/include" \
    -DGMP_LIBRARIES="${GMP_PREFIX}/lib/libgmp.dylib" \
    -DGMPXX_INCLUDE_DIR="${GMP_PREFIX}/include" \
    -DGMPXX_LIBRARIES="${GMP_PREFIX}/lib/libgmpxx.dylib" \
    -DPYTHON_EXECUTABLE="$($PYTHON_CMD -c 'import sys; print(sys.executable)')" \
    -DPYTHON_INCLUDE_DIR="${PYTHON_INCLUDE}" \
    -DPYTHON_MODULE_EXTENSION="${PYTHON_EXT_SUFFIX}"

# Build
cmake --build . --config Release -j$(sysctl -n hw.ncpu)

# Find the built module
CHIAVDF_SO=$(find . -name "chiavdf*.so" -o -name "chiavdf*.dylib" | head -1)

if [ -z "$CHIAVDF_SO" ]; then
    echo "ERROR: chiavdf module not found after build"
    exit 1
fi

echo "Built: $CHIAVDF_SO"

# Copy to output
mkdir -p "${BUILD_DIR}/output/lib"
cp "$CHIAVDF_SO" "${BUILD_DIR}/output/lib/"

# Also install to site-packages for the venv if it exists
if [ -d "${BUILD_DIR}/miner-proxy-bundle/venv" ]; then
    SITE_PACKAGES=$("${BUILD_DIR}/miner-proxy-bundle/venv/bin/python" -c "import site; print(site.getsitepackages()[0])")
    cp "$CHIAVDF_SO" "$SITE_PACKAGES/" 2>/dev/null || true
    echo "Installed to venv: $SITE_PACKAGES"
fi

echo "=== chiavdf build complete ==="
echo "Output: ${BUILD_DIR}/output/lib/$(basename $CHIAVDF_SO)"
