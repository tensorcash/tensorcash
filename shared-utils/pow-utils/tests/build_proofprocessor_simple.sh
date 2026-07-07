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
FB_SCHEMAS_DIR="${FB_SCHEMAS_DIR:-${POW_UTILS_DIR}/../fb-schemas}"

echo -e "${YELLOW}Working directory: ${POW_UTILS_DIR}${NC}"
echo -e "${YELLOW}TEST_DIR: ${TEST_DIR}${NC}"
echo -e "${YELLOW}Script source: ${BASH_SOURCE[0]}${NC}"

# Step 1: Find or download flatc. Keep this pinned to the version of the
# checked-in generated headers under shared-utils/pow-utils; several sources
# include those headers by quote path, so using an arbitrary system flatc creates
# a mixed generated-header build.
echo -e "${GREEN}Looking for flatc...${NC}"
FLATC=""
EXPECTED_FLATBUFFERS_VERSION="${FLATBUFFERS_VERSION:-23.5.26}"
# Dockerfiles pass the version as a git tag ("v23.5.26"); flatc --version and
# the release-asset URL both use the bare number — normalize the leading v.
EXPECTED_FLATBUFFERS_VERSION="${EXPECTED_FLATBUFFERS_VERSION#v}"

download_expected_flatc() {
    echo -e "${YELLOW}Downloading flatc ${EXPECTED_FLATBUFFERS_VERSION}...${NC}"
    local saved_pwd
    saved_pwd="$(pwd)"
    local flatc_asset
    case "$(uname -s)" in
        Darwin) flatc_asset="Mac.flatc.binary.zip" ;;
        Linux) flatc_asset="Linux.flatc.binary.g%2B%2B-10.zip" ;;
        *)
            echo -e "${RED}Unsupported platform for automatic flatc download: $(uname -s)${NC}"
            return 1
            ;;
    esac
    mkdir -p "${BUILD_DIR}"
    cd "${BUILD_DIR}"

    rm -f flatc
    wget -q "https://github.com/google/flatbuffers/releases/download/v${EXPECTED_FLATBUFFERS_VERSION}/${flatc_asset}"
    unzip -q -o "${flatc_asset//%2B/+}"
    chmod +x flatc
    rm -f "${flatc_asset//%2B/+}"
    FLATC="${BUILD_DIR}/flatc"
    cd "${saved_pwd}"
}

if [ -f "${BUILD_DIR}/flatc" ]; then
    FLATC="${BUILD_DIR}/flatc"
elif [ -f "/usr/local/bin/flatc" ]; then
    FLATC="/usr/local/bin/flatc"
elif [ -f "/usr/bin/flatc" ]; then
    FLATC="/usr/bin/flatc"
elif [ -f "${HOME}/.local/bin/flatc" ]; then
    FLATC="${HOME}/.local/bin/flatc"
else
    download_expected_flatc
fi

FOUND_FLATC_VERSION="$("${FLATC}" --version 2>/dev/null | awk '{print $NF}' || true)"
if [ "${FOUND_FLATC_VERSION}" != "${EXPECTED_FLATBUFFERS_VERSION}" ]; then
    echo -e "${YELLOW}Ignoring flatc ${FOUND_FLATC_VERSION:-unusable}; expected ${EXPECTED_FLATBUFFERS_VERSION}${NC}"
    download_expected_flatc
fi

echo -e "${GREEN}Using flatc: ${FLATC}${NC}"
FLATC_VERSION="$("${FLATC}" --version | awk '{print $NF}')"
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

# Check if FlatBuffers headers exist and match the flatc that generated code.
flatbuffers_header_matches_flatc() {
    local base="$1/flatbuffers/base.h"
    [ -f "${base}" ] || return 1
    local major minor revision
    major="$(grep -E '^#define FLATBUFFERS_VERSION_MAJOR ' "${base}" | awk '{print $3}')"
    minor="$(grep -E '^#define FLATBUFFERS_VERSION_MINOR ' "${base}" | awk '{print $3}')"
    revision="$(grep -E '^#define FLATBUFFERS_VERSION_REVISION ' "${base}" | awk '{print $3}')"
    [ "${major}.${minor}.${revision}" = "${FLATC_VERSION}" ]
}

MATCHED_FLATBUFFERS_INCLUDE=""
for candidate in /opt/homebrew/include /usr/local/include /usr/include "${FLATBUFFERS_INCLUDE}"; do
    if flatbuffers_header_matches_flatc "${candidate}"; then
        MATCHED_FLATBUFFERS_INCLUDE="${candidate}"
        break
    fi
done

if [ -n "${MATCHED_FLATBUFFERS_INCLUDE}" ]; then
    FLATBUFFERS_INCLUDE="${MATCHED_FLATBUFFERS_INCLUDE}"
    echo -e "${GREEN}Using FlatBuffers headers matching flatc ${FLATC_VERSION}: ${FLATBUFFERS_INCLUDE}${NC}"
else
    echo -e "${YELLOW}FlatBuffers headers not found, downloading...${NC}"
    mkdir -p "${FLATBUFFERS_INCLUDE}"
    cd "${BUILD_DIR}"

    rm -rf flatbuffers-headers/flatbuffers "flatbuffers-${FLATC_VERSION}"
    wget -q "https://github.com/google/flatbuffers/archive/v${FLATC_VERSION}.tar.gz"
    tar -xzf "v${FLATC_VERSION}.tar.gz"
    cp -r "flatbuffers-${FLATC_VERSION}/include/flatbuffers" flatbuffers-headers/
    rm -rf "flatbuffers-${FLATC_VERSION}" "v${FLATC_VERSION}.tar.gz"
fi

# Get Python paths
PYTHON_EXEC=${PYTHON_EXEC:-python3}
PYTHON_INCLUDE=$($PYTHON_EXEC -c "from sysconfig import get_paths; print(get_paths()['include'])")
PYBIND_INCLUDE=$($PYTHON_EXEC -c "import pybind11; print(pybind11.get_include())")
PYTHON_CONFIG="${PYTHON_EXEC}-config"
if [ -x "${PYTHON_CONFIG}" ]; then
    PYTHON_LDFLAGS="$("${PYTHON_CONFIG}" --ldflags)"
else
    PYTHON_LDFLAGS="$($PYTHON_EXEC - <<'PY'
import sys
import sysconfig

if sys.platform == "darwin":
    # Python extension modules on macOS leave Python symbols unresolved and
    # bind them from the hosting interpreter at import time.
    flags = ["-undefined", "dynamic_lookup"]
else:
    flags = []
    for key in ("LDFLAGS", "LIBS", "SYSLIBS"):
        value = sysconfig.get_config_var(key)
        if value:
            flags.append(value)
print(" ".join(flags))
PY
)"
fi

# Check for OpenSSL
OPENSSL_INCLUDE=""
OPENSSL_LINK=""
for prefix in /usr /usr/local /opt/homebrew/opt/openssl@3.4 \
              /opt/homebrew/opt/openssl@3 /opt/homebrew/opt/openssl \
              /usr/local/opt/openssl@3 /usr/local/opt/openssl; do
    if [ -f "${prefix}/include/openssl/sha.h" ]; then
        OPENSSL_INCLUDE="${prefix}/include"
        if [ -d "${prefix}/lib" ]; then
            OPENSSL_LINK="-L${prefix}/lib"
        fi
        break
    fi
done
if [ -z "${OPENSSL_INCLUDE}" ]; then
    echo -e "${RED}OpenSSL headers not found${NC}"
    exit 1
fi

# Check for ZMQ headers
ZMQ_INCLUDE=""
ZMQ_LINK=""
for prefix in /usr /usr/local /opt/homebrew; do
    if [ -f "${prefix}/include/zmq.h" ] && [ -f "${prefix}/include/zmq.hpp" ]; then
        ZMQ_INCLUDE="${prefix}/include"
        if [ -d "${prefix}/lib" ]; then
            ZMQ_LINK="-L${prefix}/lib"
        fi
        break
    fi
done
if [ -z "${ZMQ_INCLUDE}" ]; then
    echo -e "${RED}ZMQ C and C++ headers not found (need zmq.h and zmq.hpp)${NC}"
    exit 1
fi

# Check for libargon2 (v3 admission puzzle, TIP-0003).
# DETERMINISTIC policy — no "dynamic fallback maybe works":
#   * Linux (all Docker builders): DYNAMIC link, and every runtime image that
#     ships proof_processor.so MUST apt-install libargon2-1. Static is NOT
#     possible: distro libargon2.a is built without -fPIC and cannot be
#     linked into a shared object (ld: relocation R_X86_64_PC32 ... recompile
#     with -fPIC — observed on Ubuntu 22.04).
#   * macOS (dev): homebrew's PIC static archive when present, else dynamic.
#   * Missing argon2 entirely fails the build unless POW_V3_ALLOW_NO_ARGON2=1
#     (explicit v2-only dev build); the resulting .so cannot mine v3 (the
#     runtime startup self-test refuses POW_PROOF_VERSION=3 with no grinder).
ARGON2_INCLUDE=""
ARGON2_LIBDIR=""
ARGON2_CFLAGS=""
ARGON2_LINK=""
for prefix in /usr /usr/local /opt/homebrew/opt/argon2; do
    if [ -f "${prefix}/include/argon2.h" ]; then
        ARGON2_INCLUDE="${prefix}/include"
        if [ -d "${prefix}/lib" ]; then
            ARGON2_LIBDIR="${prefix}/lib"
        fi
        break
    fi
done
if [ -n "${ARGON2_INCLUDE}" ]; then
    ARGON2_CFLAGS="-DPOW_V3_HAVE_ARGON2"
    if [ "$(uname -s)" = "Darwin" ]; then
        ARGON2_STATIC=""
        for lib in /opt/homebrew/opt/argon2/lib/libargon2.a \
                   /usr/local/opt/argon2/lib/libargon2.a; do
            if [ -f "${lib}" ]; then
                ARGON2_STATIC="${lib}"
                break
            fi
        done
        if [ -n "${ARGON2_STATIC}" ]; then
            ARGON2_LINK="${ARGON2_STATIC}"
            echo -e "${GREEN}libargon2 found (static, macOS) — v3 admission enabled${NC}"
        else
            ARGON2_LINK="${ARGON2_LIBDIR:+-L${ARGON2_LIBDIR} }-largon2"
            echo -e "${GREEN}libargon2 found (dynamic, macOS) — v3 admission enabled${NC}"
        fi
    else
        # Linux: distro libargon2.a is non-PIC and cannot enter a .so —
        # dynamic is the ONLY correct link; runtime images MUST install
        # libargon2-1 (enforced in the Dockerfiles' runtime stages).
        ARGON2_LINK="${ARGON2_LIBDIR:+-L${ARGON2_LIBDIR} }-largon2"
        echo -e "${GREEN}libargon2 found (dynamic) — v3 admission enabled;"
        echo -e "runtime image must install libargon2-1${NC}"
    fi
else
    if [ "${POW_V3_ALLOW_NO_ARGON2:-0}" = "1" ]; then
        echo -e "${YELLOW}libargon2 not found — v3 admission (Argon2id) DISABLED"
        echo -e "(POW_V3_ALLOW_NO_ARGON2=1: explicit v2-only build)${NC}"
    else
        echo -e "${RED}libargon2 not found. Install libargon2-dev, or set"
        echo -e "POW_V3_ALLOW_NO_ARGON2=1 for an explicit v2-only build.${NC}"
        exit 1
    fi
fi

echo -e "${GREEN}Using generated headers from build directory...${NC}"

echo -e "${GREEN}Compiling libproofpack...${NC}"
# Compile libproofpack as object file
g++ -O3 -Wall -std=c++17 -fPIC -c \
    -I"${FLATBUFFERS_INCLUDE}" \
    -I"${GENERATED_CPP_DIR}" \
    -I"${OPENSSL_INCLUDE}" \
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
    -I"${ZMQ_INCLUDE}" \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/pow_utils.cpp" \
    -o "${BUILD_DIR}/pow_utils.o"

echo -e "${GREEN}Compiling pow_v3 (v3 admission/carrier helpers)...${NC}"
g++ -O3 -Wall -std=c++17 -fPIC -c \
    ${ARGON2_CFLAGS} \
    ${ARGON2_INCLUDE:+-I"${ARGON2_INCLUDE}"} \
    -I"${OPENSSL_INCLUDE}" \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/pow_v3.cpp" \
    -o "${BUILD_DIR}/pow_v3.o"

echo -e "${GREEN}Compiling bcred_table_r1024 (v3 B_cred table)...${NC}"
g++ -O3 -Wall -std=c++17 -fPIC -c \
    -I"${POW_UTILS_DIR}" \
    "${POW_UTILS_DIR}/bcred_table_r1024.cpp" \
    -o "${BUILD_DIR}/bcred_table_r1024.o"

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
    "${BUILD_DIR}/pow_v3.o" \
    "${BUILD_DIR}/bcred_table_r1024.o" \
    -o "${BUILD_DIR}/proof_processor.so" \
    $($PYTHON_EXEC -m pybind11 --includes) \
    ${PYTHON_LDFLAGS} \
    ${ZMQ_LINK} -lzmq ${OPENSSL_LINK} -lssl -lcrypto ${ARGON2_LINK}

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
