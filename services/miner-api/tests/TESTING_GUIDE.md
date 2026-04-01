# Testing Guide for Miner-API

This guide explains how to set up and run tests for the miner-api service, including handling of external dependencies like ChiaVDF and shared utilities.

## Quick Start

### Running Tests with Isolated Mocks (Recommended)

The easiest way to run tests without any external dependencies:

```bash
cd services/miner-api/tests
python3 run_isolated_tests.py
```

This will:
- Create temporary mock modules for all dependencies
- Run the mock-compatible test suites (`test_context.py` and `test_simple.py`)
- Clean up automatically

## Test Setup Options

### 1. Isolated Test Runner (`run_isolated_tests.py`)

**Pros:**
- No external dependencies needed
- Creates all mocks in a temporary directory
- Automatic cleanup
- Fast, deterministic execution

**Usage:**
```bash
python3 run_isolated_tests.py [--verbose]
```

**What it mocks:**
- `utils.uint256_arithmetics` - Arithmetic functions (`set_compact`, `get_compact`, `adjust_nbits_by_multiplier`)
- `utils.pow_utils` - PoW utilities (`calculate_hash`, `verify_pow`)
- `chiavdf` - VDF proof generation (`prove_n_wesolowski`, `create_discriminant`)
- `config.constants` - Configuration constants (sampling bounds and defaults)

The runner inserts the mock directory ahead of `src/` on `sys.path`, sets `TEST_MODE=true`, loads `test_context` and `test_simple`, runs them under `unittest`, and removes the mock directory when finished.

### 2. Environment Setup Script (`setup_test_env.py`)

**Pros:**
- Copies real shared utilities when available
- Can generate real Flatbuffers bindings
- Configurable (skip certain components)
- Suitable for integration testing

**Usage:**
```bash
# Full setup with all components
python3 setup_test_env.py

# Skip specific components
python3 setup_test_env.py --skip-chiavdf --skip-flatbuffers

# Run tests immediately after setup
python3 setup_test_env.py --run-tests

# Cleanup only
python3 setup_test_env.py --cleanup-only
```

**What it does:**
1. Copies `shared-utils/pow-utils` to `src/utils/`
2. Generates Flatbuffers Python bindings (if `flatc` available)
3. Creates ChiaVDF mock module
4. Installs Python test dependencies
5. Creates pytest configuration
6. Automatic cleanup on exit

### 3. Shell Setup Script (`setup_test_env.sh`)

**Pros:**
- Bash-based for Unix/Linux systems
- Colored output for better visibility
- Trap-based cleanup

**Usage:**
```bash
# Make executable and run
chmod +x setup_test_env.sh
./setup_test_env.sh [--skip-chiavdf] [--skip-flatbuffers]

# Run with tests
RUN_TESTS=true ./setup_test_env.sh

# Cleanup only
./setup_test_env.sh --cleanup
```

## Dependency Handling Strategies

### ChiaVDF Library

Since ChiaVDF requires compilation with specific dependencies (GMP, FLINT, etc.), the test harness provides mock implementations:

**Mock Features:**
- `prove_n_wesolowski()` - Returns a mock prover
- `MockProver.prove()` - Returns deterministic fake proofs
- `create_discriminant()` - Returns a hash-based discriminant

**Using Real ChiaVDF:**
If you need real VDF functionality:
1. Install build dependencies: `apt-get install nasm yasm build-essential cmake libgmp-dev libflint-dev`
2. Build ChiaVDF: `pip install chiavdf`
3. Skip mock creation: `--skip-chiavdf` flag

### Shared Utilities

The codebase references utilities from `shared-utils/`:

**Mock Strategy:**
- Create minimal implementations in a temp directory
- Add to the Python path before the src directory
- Clean up after tests

**Using Real Utils:**
- Ensure `shared-utils/pow-utils/` exists in project root
- Run `setup_test_env.py` without `--skip` flags

### Flatbuffers

For serialization/deserialization:

**Mock Strategy:**
- Create simple dict-based mocks for testing logic
- Skip actual Flatbuffers compilation

**Using Real Flatbuffers:**
1. Install flatc: `apt-get install flatbuffers-compiler`
2. Run setup without `--skip-flatbuffers`
3. Generates Python bindings from `.fbs` schemas

## Test Coverage

### Components Covered by the Isolated Runner

1. **LockFreeContext** (`test_context.py`)
   - Initialization
   - Mining updates
   - VDF updates (including overwrite behaviour)
   - Thread safety under concurrent updates
   - Status reporting

2. **MiningSnapshot** (`test_context.py`)
   - Snapshot creation
   - Immutability

3. **RequestPriorityManager** (`test_simple.py`)
   - Request registration
   - 1-for-1 dummy abortion logic for external requests
   - Capacity limit enforcement
   - Minimum concurrency protection

4. **Data Structures** (`test_simple.py`)
   - `RequestType` enum
   - `RequestInfo` dataclass

### Components Requiring a Full Environment

1. **RequestManager/Proxy**
   - Needs: Model client, HTTP client/server
   - Tests in `test_proxy.py` / `test_proxy_with_priority.py`

2. **VDFService**
   - Needs: Real or mock ChiaVDF
   - Tests in `test_vdf_service.py`

3. **ZMQListener**
   - Needs: ZeroMQ, Flatbuffers
   - Tests require network setup (`test_zmq_listener.py`)

## Running Specific Test Suites

### Unit Tests Only
```bash
python3 -m pytest test_context.py test_simple.py -v
```

### With Coverage
```bash
python3 -m pytest --cov=../src --cov-report=html
```

### E2E Tests (Docker Required)
```bash
docker-compose -f docker-compose.test.yml up --build
```

## Troubleshooting

### ImportError: No module named 'utils'

**Solution:**
```bash
python3 run_isolated_tests.py  # Uses mocks
# OR
python3 setup_test_env.py --run-tests  # Copies real utils
```

### ImportError: No module named 'chiavdf'

**Solution:**
```bash
# Use mock (recommended for tests)
python3 run_isolated_tests.py

# Or install real ChiaVDF
pip install chiavdf  # Requires build dependencies
```

### flatc: command not found

**Solution:**
```bash
# Skip Flatbuffers generation
python3 setup_test_env.py --skip-flatbuffers

# Or install flatc
apt-get install flatbuffers-compiler
```

## Best Practices

1. **Use Isolated Tests for CI/CD**
   - Fast, deterministic, no external deps
   - `python3 run_isolated_tests.py`

2. **Use Full Setup for Integration Testing**
   - When testing with real components
   - `python3 setup_test_env.py --run-tests`

3. **Clean Up After Testing**
   - Automatic with both scripts
   - Manual: `python3 setup_test_env.py --cleanup-only`

4. **Mock External Services**
   - GPU operations
   - Network services
   - File system operations

## Adding New Tests

1. **Create test file:** `test_<component>.py`
2. **Import from src:** Add to `sys.path` in the test
3. **Mock dependencies:** Create in temp dir or use existing mocks
4. **Run with:** `python3 run_isolated_tests.py`

## Continuous Integration

For CI/CD pipelines:

```yaml
# GitHub Actions example
- name: Run Tests
  run: |
    cd services/miner-api/tests
    python3 run_isolated_tests.py
```

## Conclusion

The testing infrastructure provides flexible options for different scenarios:
- **Quick validation:** Use isolated mocks
- **Integration testing:** Use full setup
- **CI/CD:** Use the isolated runner

The isolated runner validates the core proxy and priority-management logic — thread-safe context management, priority-based request handling, 1-for-1 dummy request abortion, capacity-limit enforcement, and minimum-concurrency maintenance — without any external dependencies.
