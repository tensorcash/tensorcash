# Shared Utils Test Strategy (pow-utils)

This document describes how the `shared-utils/pow-utils` proof-of-work utilities are
tested. The goal is **deterministic cross-language parity** between the Python and C++
implementations of the PoW sampling, serialization, and difficulty-arithmetic code, plus
correctness of the supporting data structures (ring buffers, row manager, FlatBuffers
serialization) and the verification-side unpack path.

## Scope

The pow-utils library provides the PoW sampling, hashing, and proof-serialization
primitives shared by the mining and verification components. Tests cover:

- the core Python implementation (`pow_utils.py`, `zmq_pow_writer.py`,
  `uint256_arithmetics.py`, `common_sampler_helper.py`);
- the C++ implementation (`pow_utils.{h,cpp}`, `pow_zmq_writer.{h,cpp}`) and its unit
  test (`pow_test.cpp`);
- the FlatBuffers schemas in `shared-utils/fb-schemas/*.fbs` and the generated Python
  (under `shared-utils/fb-schemas/proof/`) and C++ (`*_generated.h` alongside the
  pow-utils sources);
- the verification-side pybind11 extension (`pfunpack`) that unpacks serialized proofs;
- chiavdf build sanity and its bundled verifier tests.

## Code inventory (pow-utils)

- **Python (core):** `pow_utils.py`, `zmq_pow_writer.py`, `uint256_arithmetics.py`,
  `common_sampler_helper.py`
- **Python (utilities):** `debug_uvalue.py`, `__init__.py`
- **Python (manual tests):** under `manual-tests/`
- **C++:** `pow_utils.{h,cpp}`, `pow_zmq_writer.{h,cpp}`, tests `pow_test.cpp`,
  `compile_test.cpp`, `debug_messages.cpp` + `Makefile`
- **C++ (headers):** `any_map_dump.h`
- **FlatBuffers schemas:** `shared-utils/fb-schemas/*.fbs`, with generated C++ `*_generated.h`
  (alongside the pow-utils sources) and generated Python under `shared-utils/fb-schemas/proof/`

## Test goals

- **Deterministic cross-language parity:** byte conversions, SHA-256 message layout,
  u-value mapping, token sampling index, and nBits/target conversions must produce
  byte-identical results in Python and C++.
- **Data-structure correctness:** ring buffers, row management, and sampling telemetry
  capture.
- **FlatBuffers serialization integrity:** `Proof` → `MiningResponse` (writer) and the
  inverse (`pfunpack.unpack_proof`), across Python and C++.
- **ZMQ publishing behavior:** core-node channel vs. proxy-only audit channel, including
  the optional save-to-disk path.
- **Difficulty-arithmetic correctness:** `get_compact`/`set_compact` and target
  adjustments.
- **Build sanity** for chiavdf and the `pfunpack` extension against a pinned FlatBuffers
  version.

## Python test plan (pytest)

CPU-only; no GPU required.

- `hex_to_bytes_tensor`, `_tok_le_bytes`, `_u32le`, `_str_bytes`, `_digest_to_u` against
  known vectors (`test_byte_conversions.py`).
- `_build_msg`, `sha256_many` with determinism checks; verify that `compute_precision` is
  part of the hash input (`test_sha256_message.py`).
- `check_hash_against_target` boundary cases; `nbits_to_target` and compact conversions
  cross-checked with Bitcoin reference vectors (`test_difficulty_arithmetic.py`).
- `RowManager` allocation/free and oldest-row selection with LRU eviction
  (`test_row_manager.py`).
- `RingBuffers` clear/increment/window: circular-buffer behavior and batch independence
  (`test_ring_buffers.py`).
- `serialize_proof` with various data types, edge cases, and float32 precision
  (`test_proof_writer.py`).
- `PowHasher.update_from_payload` and payload caching; header-prefix extraction
  (`test_pow_hasher.py`).
- `pfunpack.unpack_proof` roundtrip: unpacked fields and dtypes must match the original
  dict (`test_pfunpack_roundtrip.py`; requires the built `pfunpack` extension).
- `zmq_pow_writer.MiningResponseWriter`: bind an inproc/localhost PULL socket, submit a
  minimal proof (proxy_only True/False), and validate the FlatBuffer fields via the
  generated Python classes; exercise the save-to-disk path with a tmpdir
  (`test_zmq_writer.py`).
- `uint256_arithmetics`: property-style checks that `get_compact(set_compact(x)) ≈ x`
  across random 256-bit values, plus multiplier/divider adjustment invariants
  (`test_uint256_arithmetics.py`).
- `CommonSamplerHelper`: sequence-cache initialization, context-window extraction, and
  ring-buffer management in the sampling context (`test_common_sampler_helper.py`).

### Integration triangles

- **Python → C++:** generate proof bytes with the Python serializer and unpack them via
  `pfunpack.unpack_proof`, comparing fields.
- **C++ → Python:** decode a buffer produced by the C++ `MiningResponseWriter` using the
  generated `MiningResponse`/`Proof` Python classes.

### Coverage

Use `pytest-cov` to target `shared-utils/pow-utils/*.py`. Skip GPU-only codepaths by
setting the device to CPU explicitly.

## C++ test plan (make or CMake)

- **Dependencies:** OpenSSL (libssl, libcrypto), libzmq, FlatBuffers (lib + `flatc`).
- **Build:** the bundled `Makefile` compiles `pow_utils.cpp` + `pow_zmq_writer.cpp` +
  `pow_test.cpp` into the `pow_test` binary, linking OpenSSL, libzmq, and FlatBuffers.
  `pow_utils.cpp` includes `pow_zmq_writer.h` and drives the mining-response submitter, so
  the ZMQ writer is part of the build.
- `pow_test.cpp` covers conversions, SHA-256, message building, token sampling, target
  check, and ring-buffer ops. It is extended with:
  - `nbits_to_target` and `get_compact` equivalence vs. the Python
    `uint256_arithmetics.get_compact`;
  - a FlatBuffers roundtrip: construct a minimal `Proof` with fixed vectors and confirm
    the bytes parse back via the generated C++ accessors;
  - exercising `MiningResponseWriter::serialize_response` (defined in `pow_zmq_writer.cpp`,
    which the build links) with a synthetic `proof_dict` equivalent (the C++ side uses
    `std::any`), validating the buffer identifier and minimal field set.
- Optional coverage build (GCC/Clang) with `-fprofile-arcs -ftest-coverage`, captured
  with `lcov`/`gcovr`.

## Chiavdf build + tests

Build the wheel once in a builder environment:

- Install GMP from source (6.3.0) with asm; set `GMP_USE_ASM=1`, `FLINT_ENABLE_ASM=1`,
  and leave `CHIAVDF_NO_ASM` unset to enable asm.
- `pip wheel shared-utils/chiavdf -w /tmp/wheels`, then `pip install` the wheel.

Run the bundled chiavdf tests:

- `pytest shared-utils/chiavdf/tests/test_verifier.py -q`
- `pytest shared-utils/chiavdf/tests/test_streaming_verifier.py -q`
- `pytest shared-utils/chiavdf/tests/test_n_weso_verifier.py -q`

Run on x86_64 with OpenSSL/FLINT/GMP available. For speed, `CHIAVDF_NO_ASM=1` provides a
non-asm fallback.

## FlatBuffers schema generation

Pin a single `flatc` version and use it for both Python and C++ codegen.

- `flatc --python shared-utils/fb-schemas/proof.fbs` (also `validation.fbs`,
  `blockheader.fbs`).
- `flatc --cpp shared-utils/fb-schemas/*.fbs` for the `*_generated.h` headers used by the
  C++ code.

**Consistency check:** regenerate into a temp dir and `diff -ru` against the committed
`shared-utils/fb-schemas/proof/*` and `*_generated.h`; fail if drift is detected.

## Verification x PoW cross-checks

Build the `pfunpack` extension with the same FlatBuffers install used for codegen, import
it in Python, and:

- unpack a proof produced by the Python serializer and assert field-by-field equality,
  including precise float32 casts for logits/stats;
- unpack a `validation.fbs` sample (request envelope) and confirm the nested `pow_blob`
  bytes re-parse as a `Proof`.

## ZMQ publish tests

- **Python:** start `MiningResponseWriter`, bind a local `zmq.PULL` socket on an ephemeral
  port, and set `POW_PROXY_ENABLE` / `POW_SAVE_TO_DISK` (and `MINER_LOG_DIR` for the disk
  path). Submit both the proxy-only and solution paths, decode the received messages with
  the generated Python FlatBuffers classes, and assert the envelopes and nested `Proof`
  fields.
- **C++ (optional):** the same pattern using `zmq::socket_t` to bind and read one message
  serialized by `MiningResponseWriter`.

## Continuous integration

A GitHub Actions workflow runs the pow-utils suite on `ubuntu-latest` across multiple
Python versions:

1. Install build deps: `build-essential cmake ninja pkg-config libssl-dev libzmq3-dev
   libboost-all-dev libflint-dev`.
2. Install FlatBuffers (pinned `flatc` binary or source build) and `pip install
   flatbuffers` for the Python decoders.
3. Install Python deps: `pytest pytest-cov pybind11 numpy` and a CPU-only `torch`.
4. Regenerate FlatBuffers and run the drift check.
5. Build the `pfunpack` extension in an isolated build dir against the installed
   FlatBuffers and add it to `PYTHONPATH`.
6. Run the Python tests under `shared-utils/pow-utils` with coverage.
7. Compile and run the C++ tests (`pow_utils.cpp` + `pow_test.cpp`, plus
   `pow_zmq_writer.cpp` when linking ZMQ).
8. Optionally build chiavdf without asm and run its three verifier tests.
9. Run the C++/Python cross-language comparison (`compare_cpp_python.py`) after the C++
   build.

## Local dev quickstart

**Python (from repo root):**

- Quick run: `cd shared-utils/pow-utils/tests && ./test_runner.sh`
- Core verification (no dependencies): `python3 test_simple_verify.py`
- Full setup:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install torch pytest pytest-cov flatbuffers pybind11 numpy`
  - Build `pfunpack` (the extension reads `FLATBUFFERS_INSTALL_DIR`, default
    `/usr/local`; the generated `*_generated.h` must be regenerated alongside
    `pfunpack.cpp` first — see the FlatBuffers codegen section):
    `cmake -S shared-utils/pow-utils/pfunpack -B /tmp/pfunpack && cmake --build /tmp/pfunpack -j`
  - `export PYTHONPATH=$PYTHONPATH:/tmp/pfunpack`
  - `pytest shared-utils/pow-utils/tests -v --disable-warnings --cov=shared-utils/pow-utils --cov-report=term-missing`

**C++:** `cd shared-utils/pow-utils && make clean && make` (with OpenSSL/FlatBuffers/libzmq
dev packages installed), then run `./pow_test`.

## Assumptions and notes

- **Determinism:** tests avoid RNG; PoW sampling uses cryptographic digests. The
  `compute_precision` string must be included in the message bytes in both
  implementations.
- **FlatBuffers floats:** explicitly cast to float32 for stable bytes in both Python and
  C++.
- **ZMQ linkage:** `pow_utils.cpp` includes `pow_zmq_writer.h` and drives the
  mining-response submitter, so the C++ test build links `pow_zmq_writer.cpp` and requires
  libzmq.
- **Env toggles used in tests:** `POW_PROXY_ENABLE`, `POW_SAVE_TO_DISK`, `MINER_LOG_DIR`.
