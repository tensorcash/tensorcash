# PoW Utils Test Suite

Test suite for the proof-of-work utilities, ensuring cross-language parity between the Python and C++ implementations.

## Test Coverage

The suite exercises the core building blocks of the PoW pipeline through `test_*.py` modules. Major areas covered:

- **Byte conversions** (`test_byte_conversions.py`)
  - `hex_to_bytes_tensor`: hex string to byte tensor conversion
  - `_tok_le_bytes`: token to little-endian bytes
  - `_u32le`: uint32 to little-endian bytes
  - `_str_bytes`: string to bytes conversion
  - `_digest_to_u`: digest to uniform float `[0, 1)`

- **SHA-256 message construction** (`test_sha256_message.py`)
  - `_build_msg`: message construction with all components
  - `sha256_many`: batch SHA-256 hashing
  - determinism verification
  - `compute_precision` inclusion in the hash

- **Difficulty arithmetic** (`test_difficulty_arithmetic.py`, `test_uint256_arithmetics.py`)
  - `nbits_to_target`: nBits to 256-bit target conversion
  - `check_hash_against_target`: hash validation against target
  - known Bitcoin test vectors
  - boundary conditions and edge cases

- **Row manager** (`test_row_manager.py`)
  - row allocation and deallocation
  - LRU eviction when full
  - sequence ID mapping
  - stress testing with many operations

- **Ring buffers** (`test_ring_buffers.py`)
  - circular buffer management
  - window position tracking
  - batch independence
  - data integrity across wraparound

- **Proof writer** (`test_proof_writer.py`)
  - FlatBuffers serialization
  - float32 precision handling
  - edge cases and special values
  - deterministic serialization

- **pfunpack roundtrip** (`test_pfunpack_roundtrip.py`, `test_pfunpack_comprehensive.py`)
  - Python/C++ FlatBuffers roundtrip equivalence
  - proof-field unpacking parity

Additional modules cover the PoW hasher, egress mode, ZMQ writer, adjusted-bits
equivalence, sampler helpers, and C++/Python audit-emit comparison.

## Running Tests

### Quick start
```bash
# From the repository root
cd shared-utils/pow-utils/tests
python run_tests.py
```

### With coverage
```bash
pytest . -v --cov=.. --cov-report=term-missing --cov-report=html
```

### Individual test files
```bash
pytest test_byte_conversions.py -v
pytest test_sha256_message.py -v
# etc.
```

## Requirements

- Python 3.10+
- PyTorch (the CPU build is sufficient)
- pytest, pytest-cov
- numpy
- flatbuffers

## Test Philosophy

1. **Determinism**: all operations must be deterministic and reproducible
2. **Cross-language parity**: Python and C++ must produce identical results
3. **No random data**: tests use fixed inputs for reproducibility
4. **Edge cases**: cover boundary conditions and error cases
5. **Performance**: tests should run quickly (under a second each)

## Known Test Vectors

Tests use known values from Bitcoin for validation:
- nBits conversions
- target calculations
- SHA-256 test vectors

## CI Integration

These tests run in GitHub Actions CI on pushes to the `main` and `develop`
branches (and to `test_*` working branches), on pull requests targeting `main`
or `develop`, and whenever files under `shared-utils/pow-utils/**` or
`shared-utils/fb-schemas/**` change.

The `test-pow-utils` job:

1. Sets up a Python environment
2. Installs system build dependencies (g++, cmake, libssl-dev, and the
   FlatBuffers/chiavdf toolchain)
3. Downloads the FlatBuffers `flatc` binary (v23.5.26) and generates headers
4. Builds `pfunpack.so` for cross-language testing
5. Runs the Python test suite with coverage
6. Runs the C++ minimal tests

Cross-language correctness is asserted by round-tripping FlatBuffers payloads
between the Python and C++ implementations and checking that both produce
identical bytes.
