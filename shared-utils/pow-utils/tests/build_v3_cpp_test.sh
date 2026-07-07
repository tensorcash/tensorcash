#!/usr/bin/env bash
# Build + run the standalone pow_v3 golden-vector test (TIP-0003).
#
# Works on macOS (homebrew openssl/argon2, keg or linked) and Linux (system
# packages: libssl-dev + libargon2-dev). pow_v3.{h,cpp} are self-contained —
# no flatbuffers/zmq/pybind — so this compiles even where the full
# proof_processor module cannot.
#
# Usage: tests/build_v3_cpp_test.sh   (from anywhere; paths are script-relative)
set -euo pipefail
cd "$(dirname "$0")"

# Compiler: prefer clang++ (macOS default), fall back to g++.
CXX="${CXX:-}"
if [ -z "$CXX" ]; then
    if command -v clang++ >/dev/null 2>&1; then CXX=clang++; else CXX=g++; fi
fi

INCS=()
LIBS=()

# OpenSSL: homebrew keg-only prefixes first, then system paths.
for p in /opt/homebrew/opt/openssl@3.4 /opt/homebrew/opt/openssl@3 \
         /opt/homebrew/opt/openssl /usr/local/opt/openssl@3 \
         /usr/local/opt/openssl; do
    if [ -f "$p/include/openssl/sha.h" ]; then
        INCS+=("-I$p/include"); LIBS+=("-L$p/lib"); break
    fi
done

# libargon2: homebrew prefix, else assume system include/lib paths.
for p in /opt/homebrew/opt/argon2 /usr/local/opt/argon2; do
    if [ -f "$p/include/argon2.h" ]; then
        INCS+=("-I$p/include"); LIBS+=("-L$p/lib"); break
    fi
done

# Regenerate the embedded vector header from the golden JSON (pure
# transcription; the JSON stays the single source of truth).
python3 gen_v3_vectors_header.py

mkdir -p build
OUT=build/test_pow_v3_cpp
"$CXX" -std=c++17 -O2 -Wall -Wextra \
    -DPOW_V3_HAVE_ARGON2 \
    test_pow_v3_cpp.cpp ../pow_v3.cpp ../bcred_table_r1024.cpp \
    -I.. "${INCS[@]}" "${LIBS[@]}" \
    -lcrypto -largon2 \
    -o "$OUT"

exec "$OUT"
