#!/usr/bin/env bash
# Build + run the offline-replay soak C++ cross-check (TIP-0003).
#
# Reuses the compiler/flag/lib detection of build_v3_cpp_test.sh. Regenerates the
# embedded soak header from tests/vectors/soak_grinded_cases.json (which
# tests/soak_v3_offline_replay.py must have written first), then compiles
# soak_v3_cpp_check.cpp against pow_v3.cpp + bcred_table_r1024.cpp and runs it.
#
# Usage: tests/build_soak_v3_cpp_check.sh   (from anywhere; paths script-relative)
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

# Transcribe the Python-ground soak JSON into the C++ header of literals.
python3 gen_soak_cases_header.py

mkdir -p build
OUT=build/soak_v3_cpp_check
"$CXX" -std=c++17 -O2 -Wall -Wextra \
    -DPOW_V3_HAVE_ARGON2 \
    soak_v3_cpp_check.cpp ../pow_v3.cpp ../bcred_table_r1024.cpp \
    -I.. "${INCS[@]}" "${LIBS[@]}" \
    -lcrypto -largon2 \
    -o "$OUT"

exec "$OUT"
