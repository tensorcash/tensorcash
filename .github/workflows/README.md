# GitHub Workflows

This directory contains CI/CD workflows for automated testing and building.

## Test Workflows

### kyc-prover-test.yml
**NEW**: Tests for the KYC ZK proof system including:
- Go 1.21 build for gnark circuit compiler
- Golden vector generation (BLS12-381 Groth16)
- Proof generation and verification
- Python test framework validation
- Integration with Bitcoin Core functional tests

**Jobs**:
1. `build-and-test` - Build binaries, generate vectors, test proofs
2. `test-vectors` - Verify vector integrity and structure
3. `functional-test-dry-run` - Validate test framework (without full Bitcoin node)

**Artifacts**:
- `golden-vectors` - Test vectors with proofs (30 days retention)
- `kyc-prover-binaries` - Server and gentest binaries (7 days retention)

**Duration**: ~4-5 minutes

**Note**: Does not run full functional test (requires Bitcoin Core node). Validates prover infrastructure only.

---

### test-miner-api.yml
Tests for the miner-api service including:
- Unit tests with multiple Python versions (3.8, 3.9, 3.10)
- Integration tests with mock ChiaVDF
- Docker build verification
- Coverage reporting to Codecov

### test-verification-api.yml
Tests for the verification-api service including:
- Unit tests with mocked dependencies
- Integration tests with ZMQ
- FlatBuffers generation
- Docker build verification

### ci-tests.yml
Comprehensive CI workflow that runs:
- All miner-api unit tests
- All verification-api unit tests  
- Integration tests between services
- Docker build tests for both services
- Test summary reporting

## Build Workflows

### build-docker-images.yml
Builds and publishes Docker images for all services.

### build-binaries.yml
Builds binary releases for distribution.

## Authentication

All workflows that checkout code with submodules use:
```yaml
- uses: actions/checkout@v3
  with:
    submodules: recursive
```

This ensures proper access to private submodules during CI/CD runs.

## Triggering

Test workflows trigger on:
- Push to main/develop branches
- Pull requests to main/develop
- Changes to relevant service paths
- Manual workflow dispatch (ci-tests.yml)

## Environment Variables

The workflows use the following secrets and variables:
- Codecov token (optional) - For coverage reporting

## Test Execution

Tests run in isolated environments with:
- Mocked external dependencies (ChiaVDF, torch, etc.)
- Ephemeral ports for network services
- Temporary directories for file operations
- Disabled GPU access (`CUDA_VISIBLE_DEVICES=''`)

This ensures tests are:
- Fast and reliable
- Independent of external services
- Reproducible across environments
- Safe to run in parallel