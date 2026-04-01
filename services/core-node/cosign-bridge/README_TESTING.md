# Cosign-Bridge Testing Guide

## Quick Start

### Local Testing (Recommended Before Push)

```bash
# Run all tests locally
cd services/core-node/cosign-bridge
./test-local.sh
```

This script will:
1. Check Rust formatting
2. Run clippy linter
3. Build release binary
4. Run all unit tests
5. Generate coverage report (if cargo-tarpaulin installed)
6. Check binary size

### Using Docker (CI-equivalent)

```bash
# Build
docker run --rm -v "$(git rev-parse --show-toplevel)":/workspace \
  -w /workspace/services/core-node/cosign-bridge \
  rust:1.85 \
  cargo build --release

# Test
docker run --rm -v "$(git rev-parse --show-toplevel)":/workspace \
  -w /workspace/services/core-node/cosign-bridge \
  rust:1.85 \
  cargo test --verbose -- --test-threads=1

# Coverage
docker run --rm -v "$(git rev-parse --show-toplevel)":/workspace \
  -w /workspace/services/core-node/cosign-bridge \
  rust:1.85 bash -c "\
    cargo install cargo-tarpaulin && \
    cargo tarpaulin --out Xml --output-dir target/coverage"
```

## Manual Commands

### Build

```bash
cd services/core-node/cosign-bridge

# Debug build
cargo build

# Release build (optimized)
cargo build --release

# Check compilation without building
cargo check
```

### Test

```bash
# Run all tests
cargo test

# Run tests with output
cargo test -- --nocapture

# Run specific test
cargo test test_crypto_session_creation

# Run tests in specific module
cargo test crypto::tests

# Run tests with verbose output
cargo test --verbose
```

### Linting

```bash
# Format check
cargo fmt --all -- --check

# Auto-format
cargo fmt --all

# Clippy (linter)
cargo clippy --all-targets --all-features

# Clippy with warnings as errors
cargo clippy --all-targets --all-features -- -D warnings
```

### Coverage

```bash
# Install tarpaulin (once)
cargo install cargo-tarpaulin

# Generate HTML coverage report
cargo tarpaulin --out Html --output-dir target/coverage

# Generate XML for CI/Codecov
cargo tarpaulin --out Xml --output-dir target/coverage

# Open coverage report
open target/coverage/index.html  # macOS
xdg-open target/coverage/index.html  # Linux
```

## Test Layout

Unit tests live inline (`#[cfg(test)]` modules) alongside the code they cover;
integration tests live under `tests/`. The suite totals **297 tests**
(221 unit + 76 integration).

```
cosign-bridge/
├── src/
│   ├── crypto/
│   │   ├── mod.rs        # SPAKE2 + Noise unit tests
│   │   └── test_mode.rs  # Deterministic test-mode helpers
│   ├── protocol.rs       # Frame + padding unit tests
│   ├── session.rs        # Session lifecycle unit tests
│   ├── stdio.rs          # JSON stdio protocol unit tests
│   └── transport/
│       ├── mod.rs
│       ├── websocket.rs  # WebSocket transport unit tests
│       ├── envelope.rs
│       ├── tor.rs
│       └── tor_control.rs
├── tests/
│   ├── crypto_vectors.rs            # Cross-checked crypto test vectors
│   ├── test_deterministic.rs       # Deterministic-mode integration
│   ├── websocket_integration.rs    # WebSocket transport integration
│   ├── tor_integration.rs          # Tor transport integration
│   ├── nostr_integration.rs        # Nostr relay integration
│   ├── envelope_broadcast_e2e.rs   # Envelope broadcast end-to-end
│   ├── bulletin_board_state_machine.rs
│   └── discussion_state_machine.rs # State-machine property tests
└── target/
    └── coverage/
        └── index.html   # Coverage report
```

## Test Coverage

### Crypto Module (`crypto/mod.rs`)
- Session creation
- SPAKE2 exchange (initiator/responder)
- Noise protocol initialization
- Noise handshake lifecycle
- Encrypt/decrypt operations
- SAS generation (5-word + numeric)
- Error handling (missing SPAKE2, incomplete handshake)

### Protocol Module (`protocol.rs`)
- Frame creation and serialization
- Padding buckets (256/512/1024 bytes)
- Padding application and removal
- Timestamp validation (±120s window)
- Sequence number tracking
- Message type handling
- Payload serialization

### Session Module (`session.rs`)
- Session manager creation
- Init session with invite link
- Join session via invite
- Send/recv message flow
- Rate limiting (10 msg/sec)
- Bandwidth limiting (5MB cap)
- BIP-322 attestation (challenge/verify)
- Session status queries
- Session close
- Session recovery (resume)
- Invite link parsing

### Transport Module (`transport/websocket.rs`)
- Transport creation
- Connection lifecycle
- Send message (with/without connection)
- Receive message (with/without connection)
- Close connection
- URL formatting
- Error handling

### Stdio Module (`stdio.rs`)
- All BridgeError variants
- Request deserialization
- Response serialization
- Version response
- Ping response
- Error propagation

## CI Integration

The cosign-bridge is automatically tested in CI when:
- Files in `services/core-node/cosign-bridge/**` change
- Workflow files change
- Push to `main` or `develop` branches

### CI Pipeline

1. **Detect Changes**: Monitors cosign-bridge directory
2. **Install Rust**: 1.85 toolchain with clippy + rustfmt
3. **Check Formatting**: `cargo fmt --check`
4. **Run Clippy**: Linter with warnings as errors
5. **Build Release**: Optimized binary
6. **Run Tests**: Full test suite with coverage (cargo-tarpaulin)
7. **Generate Report**: Coverage + test summary
8. **Check Binary Size**: Report final binary size

### Viewing CI Results

- **GitHub Actions**: `.github/workflows/ci.yml` (the `test-cosign-bridge` job, lines 463-586)
- **Coverage**: Reported in the CI job summary (cargo-tarpaulin)
- **Test Report**: CI summary shows all test results

## Troubleshooting

### Cargo.lock conflicts
```bash
cargo update
cargo build
```

### Test failures
```bash
# Clean and rebuild
cargo clean
cargo build
cargo test
```

### Coverage not generating
```bash
# Reinstall tarpaulin
cargo uninstall cargo-tarpaulin
cargo install cargo-tarpaulin
```

### Docker permission errors
```bash
# Add user to docker group (Linux)
sudo usermod -aG docker $USER
newgrp docker
```

## Performance Benchmarks

```bash
# Run benchmarks (if added)
cargo bench

# Profile tests
cargo test --release
```

## See Also

- Security review material: `SECURITY_AUDIT_PACKAGE.md`
- CI config: `.github/workflows/ci.yml`
- Implementation: `src/`
</content>
</invoke>
