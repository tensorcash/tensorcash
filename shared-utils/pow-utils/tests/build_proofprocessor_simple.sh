#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -e

echo "=== Building ProofProcessor with simple compilation ==="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get directories - handle both sourced and executed cases
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    # Script is being sourced
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    # Script is being executed
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

TEST_DIR="$SCRIPT_DIR"
POW_UTILS_DIR="$(dirname "$TEST_DIR")"
BUILD_DIR="${TEST_DIR}/build"
GENERATED_CPP_DIR="${BUILD_DIR}/generated-cpp"
GENERATED_PYTHON_DIR="${BUILD_DIR}/generated-python"
FLATBUFFERS_INCLUDE="${BUILD_DIR}/flatbuffers-headers"
FB_SCHEMAS_DIR="${FB_SCHEMAS_DIR:-../fb-schemas}"

echo -e "${YELLOW}Working directory: ${POW_UTILS_DIR}${NC}"
echo -e "${YELLOW}TEST_DIR: ${TEST_DIR}${NC}"
echo -e "${YELLOW}Script source: ${BASH_SOURCE[0]}${NC}"

# Step 1: Find or download flatc
echo -e "${GREEN}Looking for flatc...${NC}"
FLATC=""

if [ -f "/usr/local/bin/flatc" ]; then
    FLATC="/usr/local/bin/flatc"
elif [ -f "/usr/bin/flatc" ]; then
    FLATC="/usr/bin/flatc"
elif [ -f "${HOME}/.local/bin/flatc" ]; then
    FLATC="${HOME}/.local/bin/flatc"
elif [ -f "${BUILD_DIR}/flatc" ]; then
    FLATC="${BUILD_DIR}/flatc"
else
    echo -e "${YELLOW}flatc not found, downloading...${NC}"
    mkdir -p "${BUILD_DIR}"
    cd "${BUILD_DIR}"
    
    wget -q "https://github.com/google/flatbuffers/releases/download/v23.5.26/Linux.flatc.binary.g%2B%2B-10.zip"
    unzip -q "Linux.flatc.binary.g++-10.zip"
    chmod +x flatc
    rm -f "Linux.flatc.binary.g++-10.zip"
    FLATC="${BUILD_DIR}/flatc"
fi

echo -e "${GREEN}Using flatc: ${FLATC}${NC}"
"${FLATC}" --version || echo "No version info available"

# Step 2: Generate FlatBuffers files for both C++ and Python
echo -e "${GREEN}Generating FlatBuffers C++ headers...${NC}"
mkdir -p "${GENERATED_CPP_DIR}"
cd "${FB_SCHEMAS_DIR}"
"${FLATC}" --cpp -o "${GENERATED_CPP_DIR}" proof.fbs
"${FLATC}" --cpp -o "${GENERATED_CPP_DIR}" validation.fbs  
"${FLATC}" --cpp -o "${GENERATED_CPP_DIR}" blockheader.fbs
echo -e "${GREEN}C++ headers generated in ${GENERATED_CPP_DIR}${NC}"

echo -e "${GREEN}Generating FlatBuffers Python modules...${NC}"
mkdir -p "${GENERATED_PYTHON_DIR}"
cd "${FB_SCHEMAS_DIR}"
"${FLATC}" --python -o "${GENERATED_PYTHON_DIR}" proof.fbs
"${FLATC}" --python -o "${GENERATED_PYTHON_DIR}" validation.fbs  
"${FLATC}" --python -o "${GENERATED_PYTHON_DIR}" blockheader.fbs
echo -e "${GREEN}Python modules generated in ${GENERATED_PYTHON_DIR}${NC}"

# Check if FlatBuffers headers exist
if [ ! -d "${FLATBUFFERS_INCLUDE}/flatbuffers" ]; then
    echo -e "${YELLOW}FlatBuffers headers not found, downloading...${NC}"
    mkdir -p "${FLATBUFFERS_INCLUDE}"
    cd "${BUILD_DIR}"
    
    if [ ! -d "flatbuffers-headers/flatbuffers" ]; then
        wget -q https://github.com/google/flatbuffers/archive/v23.5.26.tar.gz
        tar -xzf v23.5.26.tar.gz
        cp -r flatbuffers-23.5.26/include/flatbuffers flatbuffers-headers/
        rm -rf flatbuffers-23.5.26 v23.5.26.tar.gz
    fi
fi

# Get Python paths
PYTHON_EXEC=${PYTHON_EXEC:-python3}
PYTHON_INCLUDE=$($PYTHON_EXEC -c "from sysconfig import get_paths; print(get_paths()['include'])")
PYBIND_INCLUDE=$($PYTHON_EXEC -c "import pybind11; print(pybind11.get_include())")

# Check for OpenSSL
OPENSSL_INCLUDE=""
if [ -d "/usr/include/openssl" ]; then
    OPENSSL_INCLUDE="/usr/include"
elif [ -d "/usr/local/include/openssl" ]; then
    OPENSSL_INCLUDE="/usr/local/include"
else
    echo -e "${RED}OpenSSL headers not found${NC}"
    exit 1
fi

# Check for ZMQ headers
ZMQ_INCLUDE=""
if [ -f "/usr/include/zmq.h" ]; then
    ZMQ_INCLUDE="/usr/include"
elif [ -f "/usr/local/include/zmq.h" ]; then
    ZMQ_INCLUDE="/usr/local/include"
else
    echo -e "${YELLOW}ZMQ headers not found, ProofProcessor will have limited functionality${NC}"
fi

echo -e "${GREEN}Using generated headers from build directory...${NC}"

echo -e "${GREEN}Compiling libproofpack...${NC}"
# Compile libproofpack as object file
g++ -O3 -Wall -std=c++17 -fPIC -c \
    -I"${FLATBUFFERS_INCLUDE}" \
    -I"${GENERATED_CPP_DIR}" \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/pfunpack/libproofpack.cpp" \
    -o "${BUILD_DIR}/libproofpack.o"

echo -e "${GREEN}Compiling pow_zmq_writer...${NC}"
# Compile pow_zmq_writer as object file
g++ -O3 -Wall -std=c++17 -fPIC -c \
    -I"${FLATBUFFERS_INCLUDE}" \
    -I"${GENERATED_CPP_DIR}" \
    -I"${ZMQ_INCLUDE}" \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/pow_zmq_writer.cpp" \
    -o "${BUILD_DIR}/pow_zmq_writer.o"

echo -e "${GREEN}Compiling pow_utils...${NC}"
# Compile pow_utils as object file (for hex_to_bytes and other utilities)
g++ -O3 -Wall -std=c++17 -fPIC -c \
    -I"${FLATBUFFERS_INCLUDE}" \
    -I"${GENERATED_CPP_DIR}" \
    -I"${OPENSSL_INCLUDE}" \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/pow_utils.cpp" \
    -o "${BUILD_DIR}/pow_utils.o"

echo -e "${GREEN}Compiling proof_processor module...${NC}"
# Compile and link proof_processor with bindings
g++ -O3 -Wall -shared -std=c++17 -fPIC \
    -I"${PYTHON_INCLUDE}" \
    -I"${PYBIND_INCLUDE}" \
    -I"${FLATBUFFERS_INCLUDE}" \
    -I"${GENERATED_CPP_DIR}" \
    -I"${OPENSSL_INCLUDE}" \
    -I"${ZMQ_INCLUDE}" \
    -I"${POW_UTILS_DIR}/pfunpack" \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/proof_processor.cpp" \
    "${POW_UTILS_DIR}/proof_processor_bindings.cpp" \
    "${BUILD_DIR}/libproofpack.o" \
    "${BUILD_DIR}/pow_zmq_writer.o" \
    "${BUILD_DIR}/pow_utils.o" \
    -o "${BUILD_DIR}/proof_processor.so" \
    $($PYTHON_EXEC -m pybind11 --includes) \
    $($PYTHON_EXEC-config --ldflags) \
    -lzmq -lssl -lcrypto

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ proof_processor.so built successfully!${NC}"
    echo -e "${YELLOW}Location: ${BUILD_DIR}/proof_processor.so${NC}"
    echo -e "${YELLOW}Python FlatBuffer modules: ${GENERATED_PYTHON_DIR}${NC}"
    
    # Test import with updated PYTHONPATH including generated Python modules
    echo -e "${GREEN}Testing proof_processor import...${NC}"
    PYTHONPATH="${BUILD_DIR}:${GENERATED_PYTHON_DIR}:${PYTHONPATH}" $PYTHON_EXEC -c "import proof_processor; print('✓ proof_processor imported successfully')"
else
    echo -e "${RED}Failed to build proof_processor.so${NC}"
    exit 1
fi

echo -e "${GREEN}=== Build complete! ===${NC}"
