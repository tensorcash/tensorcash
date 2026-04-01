# Manual Tests and Development Utilities

This directory contains manual testing scripts and development utilities that were used during the development of the PoW utilities. These are **NOT** part of the automated test suite but can be useful for debugging and manual verification.

## Files

### `verify_equivalency.py`
- **Purpose**: Manual verification script to compare C++ and Python PoW implementations
- **Usage**: Run both C++ and Python implementations with same inputs and compare outputs
- **Note**: An automated version of this is now available as `test_cross_language_equivalency.py` in the main test suite

### `pow_python_test.py`
- **Purpose**: Standalone test script for Python PoW utilities
- **Usage**: `python3 pow_python_test.py`
- **Use Case**: Quick manual testing of Python implementations without pytest framework

### `test_sha256.py`
- **Purpose**: Standalone SHA-256 implementation in PyTorch for GPU testing
- **Usage**: Direct import or standalone execution
- **Note**: The actual automated test is `test_sha256_message.py` in the tests directory

### `test_sha256_ragged.py`
- **Purpose**: Test SHA-256 with variable-length (ragged) inputs
- **Usage**: Performance and correctness testing for batched SHA-256 with different message lengths
- **Use Case**: Useful for optimizing batched hashing operations

### `debug_messages.py`
- **Purpose**: Debug utility to print exact byte sequences for troubleshooting
- **Usage**: Import functions to debug byte conversions and message building
- **Use Case**: Helpful when debugging mismatches between C++ and Python implementations

## Cross-Language Testing

**ACTUAL cross-language testing requires both C++ and Python to be working:**
- Run: `./test_cpp_python_equivalency.sh`
- This requires the C++ binary to be compiled first (`make pow_test` in parent dir)
- This compares actual outputs from both implementations

## When to Use These

Use these manual tests when:
1. Debugging specific implementation details
2. Doing performance testing/optimization
3. **Actually testing C++ vs Python equivalency** (not just Python tests)
4. Developing new features that need quick iteration

## Automated Testing

For automated testing, use the test suite in `../tests/`:
- Run all tests: `cd ../tests && ./test_runner.sh`
- Run specific test: `python3 -m pytest ../tests/test_specific.py`
- Python implementation tests: `python3 -m pytest ../tests/test_python_implementation.py`

## Note

These files are kept for reference and debugging purposes. They are not maintained as rigorously as the main test suite and may not always be up-to-date with the latest API changes.