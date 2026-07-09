#!/usr/bin/env bash
# Build + run the llama.cpp-path v3 wiring test (TIP-0003).
#
# Compiles the REAL llama miner stack — pow_utils.cpp + pow_v3.cpp +
# pow_zmq_writer.cpp — exactly as the llama-server build does, and drives
# PowSamplingCoordinator end-to-end (grind -> per-step nonce hashing ->
# FlatBuffer proof emission -> verifier-style replay).
#
# Works on macOS (homebrew: openssl, argon2, flatbuffers, zeromq, cppzmq)
# and Linux (libssl-dev, libargon2-dev, flatbuffers, libzmq3-dev). The
# checked-in *_generated.h are pinned to flatc 23.5.26; this script
# regenerates them into the build dir with the LOCAL flatc (mirroring the
# Dockerfile, which also regenerates at image build) and compiles copies of
# the sources there so quoted includes resolve to the regenerated headers.
#
# Usage: tests/build_llama_v3_wiring_test.sh   (paths are script-relative)
set -euo pipefail
cd "$(dirname "$0")"

POW_DIR="$(cd .. && pwd)"
FBS_DIR="$(cd ../../fb-schemas && pwd)"
BUILD="build/llama_v3_wiring"
mkdir -p "$BUILD"

CXX="${CXX:-}"
if [ -z "$CXX" ]; then
    if command -v clang++ >/dev/null 2>&1; then CXX=clang++; else CXX=g++; fi
fi

INCS=()
LIBS=()
for pkg in openssl@3.4 openssl@3 openssl argon2 flatbuffers zeromq cppzmq; do
    for prefix in /opt/homebrew/opt /usr/local/opt; do
        if [ -d "$prefix/$pkg/include" ]; then
            INCS+=("-I$prefix/$pkg/include")
            [ -d "$prefix/$pkg/lib" ] && LIBS+=("-L$prefix/$pkg/lib")
            break
        fi
    done
done

# Regenerate FlatBuffers headers with the local flatc (version must match
# the installed flatbuffers library, exactly like the Dockerfile build).
command -v flatc >/dev/null 2>&1 || { echo "flatc not found"; exit 1; }
flatc --cpp -o "$BUILD" "$FBS_DIR/proof.fbs" "$FBS_DIR/blockheader.fbs" \
    "$FBS_DIR/validation.fbs"

# Copy the sources next to the regenerated headers so quoted includes pick
# them up (checked-in headers in the source dir stay untouched).
cp "$POW_DIR"/pow_utils.h "$POW_DIR"/pow_utils.cpp \
   "$POW_DIR"/pow_v3.h "$POW_DIR"/pow_v3.cpp \
   "$POW_DIR"/bcred_table_r1024.h "$POW_DIR"/bcred_table_r1024.cpp \
   "$POW_DIR"/pow_zmq_writer.h "$POW_DIR"/pow_zmq_writer.cpp \
   "$POW_DIR"/pow_zmq_writer_helpers.h "$POW_DIR"/any_map_dump.h \
   test_llama_v3_wiring.cpp "$BUILD/"

OUT="$BUILD/test_llama_v3_wiring"
"$CXX" -std=c++17 -O2 -Wall -Wextra -Wno-deprecated-declarations \
    -DPOW_V3_HAVE_ARGON2 \
    "$BUILD"/test_llama_v3_wiring.cpp "$BUILD"/pow_utils.cpp \
    "$BUILD"/pow_v3.cpp "$BUILD"/bcred_table_r1024.cpp \
    "$BUILD"/pow_zmq_writer.cpp \
    -I"$BUILD" "${INCS[@]}" "${LIBS[@]}" \
    -lcrypto -lssl -largon2 -lzmq \
    -o "$OUT"

"$OUT"

# Cross-language acceptance (§10): re-verify the C++-emitted proof bins with
# the PYTHON pow_v3 reference (verify_llama_proof_bin.py). Needs the
# flatbuffers + argon2-cffi python packages; PYTHON defaults to the repo root
# venv when present.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for cand in ../../../../venv/bin/python ../../../venv/bin/python; do
        if [ -x "$cand" ]; then PYTHON="$cand"; break; fi
    done
    PYTHON="${PYTHON:-python3}"
fi
# blockheader first, proof second: flatc emits include-stub Proof.py when
# compiling blockheader.fbs (which includes proof.fbs), so proof.fbs must be
# generated LAST to keep the real Proof class.
rm -rf "$BUILD/py"
flatc --python -o "$BUILD/py" "$FBS_DIR/blockheader.fbs"
flatc --python -o "$BUILD/py" "$FBS_DIR/proof.fbs"
PROOF_SCRATCH="${TMPDIR:-/tmp}/llama_v3_wiring_test"
"$PYTHON" verify_llama_proof_bin.py --fbs-py "$BUILD/py" \
    "$PROOF_SCRATCH"/proofs_v3/*.bin "$PROOF_SCRATCH"/proofs_v2/*.bin
