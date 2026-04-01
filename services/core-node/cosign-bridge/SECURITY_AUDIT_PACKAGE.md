# Security Audit Package - Cosign Bridge

**Contact:** security@tensorcash.org

---

## Purpose

This document is a guide for security auditors reviewing the TensorCash Cosign Bridge
cryptographic implementation. The bridge lets a Bitcoin Core / Qt wallet co-sign Bitcoin
transactions with a remote party over a channel secured by a password-authenticated key
exchange (SPAKE2) and the Noise Protocol Framework. It describes the cryptographic design as
implemented, the security properties each primitive is relied on for, and the questions an
auditor should answer against the code.

Line references point at `src/crypto/mod.rs` unless noted; verify them against the tree you
are auditing.

---

## Scope of Audit

### In Scope

**Cryptographic implementation**
- SPAKE2 password-authenticated key exchange
- HKDF-SHA256 key derivation
- Noise Protocol Framework (`Noise_NNpsk0_25519_ChaChaPoly_BLAKE2b`)
- Short Authentication String (SAS) generation
- Key hygiene and memory management

**Session management**
- Session lifecycle (init, handshake, send/recv, close)
- Invite code generation and validation
- Rate limiting and bandwidth controls
- Session persistence and recovery

**Test mode**
- Test-mode isolation from production
- Production safety guarantees
- Deterministic testing infrastructure

**Error handling**
- Information-leakage prevention
- Panic-free operation
- Resource cleanup

**Side-channel resistance**
- Timing-attack resistance
- Constant-time comparisons
- Memory zeroization

### Out of Scope

**Third-party library internals**
- Internal implementation of the `spake2` crate
- Internal implementation of the `snow` crate
- Rust standard-library cryptographic primitives

**Network infrastructure**
- WebSocket relay server implementation (separate component)
- TLS/SSL certificate validation (handled by `tokio-tungstenite`)

**Bitcoin Core integration**
- Bitcoin transaction validation logic
- PSBT parsing and signing
- Bitcoin Core RPC interface

---

## Architecture Overview

### Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Bitcoin Core + Qt Wallet                 │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              Stdio JSON-RPC Interface                 │  │
│  └────────────────────┬─────────────────────────────────┘  │
└───────────────────────┼─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    Cosign Bridge (Rust)                      │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   Session    │  │    Crypto    │  │  Transport   │     │
│  │  Management  │◄─┤   (SPAKE2,   │◄─┤  (WebSocket) │     │
│  │              │  │    Noise)    │  │              │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│         │                  │                  │              │
│         └──────────────────┴──────────────────┘              │
│                           │                                  │
│                  ┌────────▼────────┐                        │
│                  │   Test Mode     │                        │
│                  │ (deterministic) │                        │
│                  └─────────────────┘                        │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
              WebSocket Relay (Optional)
```

### Protocol Flow

```
Initiator                    Responder
    │                            │
    │  1. init() → invite_code   │
    │                            │
    │◄───── invite_code ─────────┤ 2. join(invite_code)
    │                            │
    │  3. SPAKE2 Exchange        │
    │─────── spake2_msg1 ───────►│
    │◄────── spake2_msg2 ────────┤
    │                            │
    │  4. Derive Shared Secret   │
    │     (both parties)         │
    │                            │
    │  5. HKDF → Noise PSK       │
    │     (both parties)         │
    │                            │
    │  6. Noise Handshake        │
    │─────── noise_msg1 ─────────►│
    │◄────── noise_msg2 ──────────┤
    │                            │
    │  7. Generate SAS           │
    │     (both parties)         │
    │                            │
    │  8. User verifies SAS      │
    │     (out-of-band)          │
    │                            │
    │  9. Encrypted Transport    │
    │◄────── encrypt/decrypt ────►│
    │                            │
```

The invite code is the PAKE password: it both bootstraps the SPAKE2 exchange and, through
HKDF, becomes the pre-shared key bound into the Noise handshake. Both parties independently
derive the same shared secret, the same Noise PSK, the same transport keys, and the same SAS;
no key material crosses the network.

---

## Cryptographic Primitives

### 1. SPAKE2 Password-Authenticated Key Exchange

**Purpose:** establish a shared secret from the invite-code password without revealing the
password to a network attacker.

**Library:** `spake2` crate
**Group:** Ed25519 (Curve25519 in Edwards form)
**Security level:** ~128 bits

**Implementation:** `spake2_start` (`src/crypto/mod.rs:4000`), `spake2_finish`
(`src/crypto/mod.rs:4037`).

```rust
pub fn spake2_start(&mut self, is_initiator: bool) -> Result<Vec<u8>> {
    let password = Password::new(self.invite_code.as_bytes());

    let (state, outbound_msg) = if is_initiator {
        Spake2::<Ed25519Group>::start_a(
            &password,
            &Identity::new(b"initiator"),
            &Identity::new(b"responder"),
        )
    } else {
        Spake2::<Ed25519Group>::start_b(
            &password,
            &Identity::new(b"initiator"), // idA — same value and order as start_a
            &Identity::new(b"responder"), // idB — same value and order as start_a
        )
    };
    // ... store state, return outbound_msg ...
}
```

Both sides bind the exchange to fixed identity labels `b"initiator"` and `b"responder"`
(supplied as `idA`/`idB` in the same value and order on both sides), not empty identities.
`spake2_finish` consumes the stored state and the peer message to produce the shared secret.

**Security properties relied on:**
- Offline dictionary-attack resistance (SPAKE2 leaks nothing offline-testable about the password)
- The ephemeral exchange gives no static long-term key to compromise
- MITM protection is completed by out-of-band SAS verification (see §4)
- The password is never transmitted

**Audit focus:**
- Confirm `idA`/`idB` are identical and identically ordered on both `start_a` and `start_b`
  so the two sides derive the same secret.
- Confirm the invite code, used here as the password, has adequate entropy for its threat
  model (see Invite Code Generation below — it is the weakest entropy source in the system).
- Confirm the SPAKE2 crate version and check for known advisories.
- Confirm the password / invite code is never logged. Note `spake2_finish` logs the secret
  *length* only.
- Confirm error handling on malformed peer messages (`finish` returns an error rather than
  panicking).
- Confirm secret material is zeroized on drop.

### 2. HKDF Key Derivation

**Purpose:** derive the Noise PSK from the SPAKE2 shared secret with domain separation.

**Library:** `hkdf`, `sha2`
**KDF:** HKDF-SHA256
**Info string:** `cosign-noise-psk-v1` (domain separation)

**Implementation:** `derive_noise_psk` (`src/crypto/mod.rs:3917`), called from `init_noise`.

```rust
fn derive_noise_psk(shared_secret: &[u8]) -> Result<Vec<u8>> {
    let hkdf = Hkdf::<Sha256>::new(None, shared_secret);
    let mut psk = vec![0u8; 32]; // 32 bytes for Noise PSK
    hkdf.expand(b"cosign-noise-psk-v1", &mut psk)
        .map_err(|_| anyhow::anyhow!("HKDF expansion failed"))?;
    Ok(psk)
}
```

HKDF is invoked with a `None` salt; the SPAKE2 output is already uniformly distributed, and
the constant info string provides domain separation so this derivation cannot collide with a
different protocol that hashes the same secret.

**Security properties relied on:**
- Domain separation via a constant, versioned info string
- Extract-then-expand structure ensures a well-distributed 32-byte key
- SHA-256 collision/preimage resistance

**Audit focus:**
- Confirm the info string is constant and versioned (`cosign-noise-psk-v1`).
- Confirm the expand output length is 32 bytes.
- Confirm the PSK is zeroized after use.
- Confirm HKDF output is not reused across sessions (a fresh SPAKE2 secret feeds each one).

### 3. Noise Protocol Framework

**Purpose:** establish a forward-secure, mutually authenticated encrypted channel.

**Library:** `snow`
**Pattern:** `Noise_NNpsk0_25519_ChaChaPoly_BLAKE2b`
- **NN:** no static keys (ephemeral-only)
- **psk0:** pre-shared key mixed in at the start of the handshake (the HKDF-derived PSK)
- **25519:** X25519 Diffie-Hellman
- **ChaChaPoly:** ChaCha20-Poly1305 AEAD
- **BLAKE2b:** handshake hash

**Implementation:** `init_noise` (`src/crypto/mod.rs:4068`),
`noise_handshake_step` (`src/crypto/mod.rs:4109`),
`noise_handshake_write` (`src/crypto/mod.rs:4149`),
`encrypt` (`src/crypto/mod.rs:4193`), `decrypt` (`src/crypto/mod.rs:4217`).

The pattern string is supplied inline inside `init_noise`:

```rust
// Use Noise_NNpsk0 pattern which doesn't require static keys
// NN = no static keys, psk0 = PSK at beginning
let params = "Noise_NNpsk0_25519_ChaChaPoly_BLAKE2b"
    .parse()
    .map_err(|e| anyhow::anyhow!("Invalid Noise pattern: {:?}", e))?;

let builder = Builder::new(params).psk(0, &psk);

let noise_state = if is_initiator {
    builder.build_initiator()?
} else {
    builder.build_responder()?
};
```

`noise_handshake_step` / `noise_handshake_write` drive the handshake; when
`is_handshake_finished()` returns true they capture the handshake hash
(`get_handshake_hash()`) into `self.handshake_hash` *before* calling `into_transport_mode()`,
so the hash is available for SAS derivation. After the handshake the session holds a
`TransportState` and `encrypt`/`decrypt` route plaintext/ciphertext through it.

> **Auditor note — dead constant.** A module-level constant
> `NOISE_PATTERN = "Noise_XX_25519_ChaChaPoly_BLAKE2b"` (`src/crypto/mod.rs:17`) is **not the
> pattern in force.** It is unused; the live handshake uses the inlined
> `Noise_NNpsk0_25519_ChaChaPoly_BLAKE2b` string in `init_noise`. Do not audit the channel as
> an `XX` handshake — it is `NNpsk0`. The constant should be treated as misleading and is a
> candidate for removal.

**Security properties relied on:**
- Forward secrecy from ephemeral X25519 keys
- Authenticated encryption via ChaCha20-Poly1305
- Replay protection via the AEAD's monotonic nonce sequence
- MITM protection from PSK binding plus out-of-band SAS verification

**Audit focus:**
- Confirm the live pattern is `NNpsk0` and the dead `NOISE_PATTERN` constant is not
  accidentally wired in elsewhere.
- Confirm the PSK is supplied at position 0 (`psk(0, ...)`).
- Confirm the handshake-hash capture happens on both the read (`noise_handshake_step`) and
  write (`noise_handshake_write`) completion paths.
- Confirm `encrypt`/`decrypt` reject calls made before transport mode is reached
  (they `bail!("Handshake not complete")`).
- Confirm handshake messages cannot be replayed into an established session
  (`bail!("Handshake already complete")`).
- Confirm error handling on malformed handshake/transport messages.

### 4. Short Authentication String (SAS)

**Purpose:** let the two human operators confirm, out of band, that no MITM sat in the
handshake.

**Derivation:** from the Noise handshake hash (post-handshake)
**Wordlist:** EFF Large Wordlist (`EFF_WORDLIST`, `src/crypto/mod.rs:22`)
**Format:** 5 words separated by hyphens (e.g. `alpha-bravo-charlie-delta-echo`)
**Entropy:** ~55 bits (5 words × 11 bits)

**Implementation:** `generate_sas` (`src/crypto/mod.rs:4241`) delegating to
`derive_sas_from_transcript` (`src/crypto/mod.rs:3927`).

```rust
fn derive_sas_from_transcript(handshake_hash: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(handshake_hash);
    let hash = hasher.finalize();

    let mut words = Vec::with_capacity(5);
    for i in 0..5 {
        let byte_idx = i * 2;                       // two bytes per word
        let two_bytes = u16::from_be_bytes([hash[byte_idx], hash[byte_idx + 1]]);
        let word_idx = (two_bytes & 0x7FF) as usize; // low 11 bits → 0..2047
        let final_idx = word_idx % EFF_WORDLIST.len().min(2048);
        words.push(EFF_WORDLIST[final_idx]);
    }
    words.join("-")
}
```

The 5 word indices are read from the first 10 bytes of `SHA256(handshake_hash)`, two bytes
per word, masking the low 11 bits of each `u16`. Because the SAS is derived solely from the
Noise handshake hash, it binds to the full handshake transcript and to the PSK that was mixed
in. `generate_sas` only uses the session-id/invite-code fallback when no handshake hash is
present (i.e. before the handshake completes); the post-handshake SAS does **not** mix in the
session id.

A 6-digit numeric variant (`generate_sas_numeric`, `src/crypto/mod.rs:4263`) exists for
display contexts that cannot show words; it derives from `session_id || invite_code` and is a
weaker, pre-handshake fallback — not the channel-authenticating SAS.

**Security properties relied on:**
- ~55 bits of entropy, adequate for human MITM detection
- Bound to the handshake hash: an attacker cannot force a matching SAS without breaking the
  Noise handshake
- Human-readable EFF wordlist for reliable verbal comparison

**Audit focus:**
- Confirm the SAS is derived from the post-handshake handshake hash, not an intermediate
  state, on the path that actually authenticates the channel.
- Confirm both parties take the same branch (handshake-hash branch) once the handshake is
  complete, so their SAS values match.
- Confirm the 11-bit index extraction and the `% 2048` bound keep indices in range.
- Confirm any SAS comparison performed by callers is constant-time.
- Confirm the SAS is shown to the user but never transmitted over the channel or logged.

---

## Test Mode Security

### Purpose

A deterministic test mode enables reproducible integration tests and test-vector generation.
It must not weaken production security.

### Implementation

**Location:** `src/crypto/test_mode.rs`

**Global state:**
```rust
static TEST_MODE: Lazy<Mutex<Option<TestModeState>>> = Lazy::new(|| Mutex::new(None));

struct TestModeState {
    rng: ChaCha20Rng,           // deterministic PRNG
    fixed_time_ms: Option<u64>, // optional fixed timestamp
}
```

**Activation:**
```rust
pub fn enable(seed: [u8; 32], fixed_time: Option<u64>) {
    let rng = ChaCha20Rng::from_seed(seed);
    *TEST_MODE.lock().unwrap() = Some(TestModeState {
        rng,
        fixed_time_ms: fixed_time,
    });
    log::warn!("⚠️  TEST MODE ENABLED - NOT FOR PRODUCTION");
}
```

**Production wrappers** (`src/crypto/test_mode.rs:87`, `:102`):
```rust
pub fn random_bytes(count: usize) -> Vec<u8> {
    if let Some(bytes) = /* test-mode RNG draw */ {
        bytes  // test mode: deterministic
    } else {
        use rand::rngs::OsRng;
        let mut bytes = vec![0u8; count];
        OsRng.fill_bytes(&mut bytes);  // production: CSPRNG
        bytes
    }
}

pub fn current_timestamp_ms() -> u64 {
    if let Some(ts) = /* test-mode fixed time */ {
        ts  // test mode: fixed timestamp
    } else {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64  // production: real time
    }
}
```

### Security Properties

**Production mode (default):**
- Test mode is disabled unless explicitly enabled.
- `random_bytes` falls through to `OsRng` (cryptographically secure).
- `current_timestamp_ms` falls through to `SystemTime::now()`.
- Full entropy, non-deterministic.

**Test mode (opt-in):**
- Uses `ChaCha20Rng` seeded from a fixed seed (deterministic).
- Optional fixed timestamp.
- Zero entropy for application-level randomness.
- Emits a warning on activation.
- Must never be enabled in production.

### Scope of Control

**What test mode controls:**
- Session-ID generation (timestamp + randomness)
- Invite-code generation (randomness)
- Message timestamps
- Application-level random data

**What test mode cannot control:**
- SPAKE2 messages (the crate uses its own RNG via `getrandom`)
- Noise ephemeral keys (downstream of SPAKE2)
- The handshake hash (varies with SPAKE2 ephemerals)
- Cross-run SAS values (derived from the handshake hash)

**What is consistent within a single run:**
- The SPAKE2 shared secret (both parties derive the same secret in one run)
- The Noise transport keys
- The SAS (both parties derive the same SAS in one run)
- Encryption/decryption once the handshake completes

### Audit Focus

- Confirm test mode is off by default (no implicit activation).
- Confirm activation is explicit and warns.
- Confirm the production wrappers check test-mode state before drawing from the test RNG, and
  use `OsRng` / real time otherwise.
- Confirm no production code path reaches the test RNG without explicit activation.
- Confirm test-mode state is thread-safe (it is `Mutex`-protected).
- Confirm test mode cannot be accidentally left enabled across a process boundary.

---

## Session Management Security

### Session Lifecycle

**States:**
1. **Uninitialized** — no session exists
2. **Initiated** — session created, waiting for peer
3. **Handshaking** — SPAKE2/Noise handshake in progress
4. **Established** — handshake complete, ready for messages
5. **Closed** — session terminated

**Implementation:** `src/session.rs`

### Session ID Generation

**Format:** `session_{nanos}_{random}`
- **nanos:** milliseconds since epoch × 1,000,000
- **random:** 4 random bytes interpreted as a big-endian `u32`

**Implementation:** `generate_session_id` (`src/session.rs:2141`).

```rust
fn generate_session_id() -> String {
    let timestamp_ms = test_mode::current_timestamp_ms();
    let nanos = timestamp_ms as u128 * 1_000_000;

    let random_bytes = test_mode::random_bytes(4);
    let random = u32::from_be_bytes([
        random_bytes[0], random_bytes[1], random_bytes[2], random_bytes[3],
    ]);

    format!("session_{}_{}", nanos, random)
}
```

**Security properties relied on:**
- Time component gives temporal uniqueness; the 32-bit random component disambiguates
  sessions created within the same millisecond.
- Unpredictable in production (`OsRng`).
- The session id is not secret and may be logged.

**Audit focus:**
- Confirm the random component comes from a CSPRNG in production.
- Confirm session ids are not reused.
- Confirm nothing treats the session id as a secret.

### Invite Code Generation

**Format:** 5 hyphen-separated words, one word per random byte.

**Implementation:** `generate_invite_code` (`src/session.rs:2161`).

```rust
fn generate_invite_code(_session_id: &str) -> String {
    const WORDS: &[&str] = &[ /* 32-word list: apple, banana, cherry, ... */ ];

    let random_bytes = test_mode::random_bytes(5); // one byte per word
    let mut words = Vec::with_capacity(5);
    for &byte in &random_bytes {
        let idx = (byte as usize) % WORDS.len();
        words.push(WORDS[idx]);
    }
    words.join("-")
}
```

> **Auditor note — entropy.** The invite-code generator draws from a small built-in 32-word
> list (one byte per word, `byte % 32`), **not** the full 2048-word EFF wordlist used by the
> SAS. With 5 words over a 32-word alphabet the code carries roughly **25 bits** of entropy
> (5 × 5 bits), not 55. Because this invite code *is* the SPAKE2 password, its entropy bounds
> the offline-guessing security of the channel. Treat the invite-code entropy as the primary
> finding to evaluate: assess whether ~25 bits is sufficient for the deployment's threat model
> (single-use, short-lived, online-only exchange) and whether the channel should adopt the
> full EFF wordlist here as the in-code comment ("replace with full EFF wordlist in
> production") suggests.

**Audit focus:**
- Quantify the invite-code entropy against the threat model (see note above).
- Confirm the random bytes come from a CSPRNG in production.
- Confirm an invite code is single-use and short-lived.
- Confirm the invite code is not logged or leaked.
- Confirm any invite-code comparison is constant-time.

### Rate Limiting

**Default:** 10 messages/second per session (`MAX_MESSAGES_PER_SECOND`, `src/session.rs`)
**Implementation:** sliding 1-second window over a queue of message timestamps
(`check_rate_limit`). Timestamps older than one second are evicted; if the window
still holds the limit or more, the call is rejected with a `retry_after_ms`.

**Security properties relied on:**
- Throttles request-flooding DoS.
- Per-session isolation; one session cannot exhaust global limits.

**Audit focus:**
- Confirm rate limiting is enforced before expensive operations.
- Confirm it cannot be bypassed.
- Confirm the configured limit is reasonable and that limit errors are handled.

### Bandwidth Limiting

**Default:** 5 MB cumulative budget per session (`MAX_SESSION_BANDWIDTH_BYTES`,
`src/session.rs`)
**Implementation:** a running per-session byte counter (`total_bandwidth_bytes`),
not a per-second rate. `check_bandwidth_limit` rejects a message when the cumulative
total plus the new payload would exceed the budget; the session is then expected to
close.

**Security properties relied on:**
- Bounds total memory/bandwidth a single session can consume.
- Per-session isolation.

**Audit focus:**
- Confirm the budget is enforced before message processing.
- Confirm the size accounted for is the intended one (plaintext vs on-wire).
- Confirm the budget is reasonable and that exceeding it is handled (session close).

---

## Test Vectors

### Purpose

Reproducible cryptographic outputs for auditing. Test vectors run under deterministic test
mode with a fixed seed.

**Test configuration:**
```rust
const TEST_SEED: [u8; 32] = [0x42; 32];
const TEST_TIME: u64 = 1609459200000; // 2021-01-01 00:00:00 UTC
const TEST_PASSWORD: &str = "golf-hotel-foxtrot-echo-hotel";
```

### Running Test Vectors

```bash
# from the cosign-bridge crate directory
cargo test --test crypto_vectors -- --nocapture > audit_vectors.txt
```

To run in a clean container, mount the repository and run the same command from
`services/core-node/cosign-bridge` inside a `rust:1.85` image.

### Test Vector Coverage

The `tests/crypto_vectors.rs` suite emits the following vectors:

**1. SPAKE2 exchange (`test_vector_spake2_exchange`)**
- Initiator and responder messages (compressed Ed25519 points)
- 32-byte shared secret

**2. HKDF PSK derivation (`test_vector_noise_psk_derivation`)**
- Input: SPAKE2 shared secret (32 bytes)
- Info: `cosign-noise-psk-v1`
- Output: 32-byte Noise PSK

**3. Noise handshake (`test_vector_noise_handshake`)**
- Pattern: `Noise_NNpsk0_25519_ChaChaPoly_BLAKE2b`
- Message 1: initiator → responder
- Message 2: responder → initiator

**4. SAS derivation (`test_vector_sas_derivation`)**
- Source: post-handshake Noise handshake hash
- Output: 5-word EFF SAS (~55 bits)

**5. Encryption/decryption (`test_vector_encryption_decryption`)**
- Plaintext round-trips through `encrypt`/`decrypt`
- Ciphertext carries the 16-byte Poly1305 tag

**6. Full protocol flow (`test_vector_full_protocol_flow`)**
- End-to-end: SPAKE2 → HKDF → Noise → SAS → transport

**7. Deterministic session id (`test_vector_deterministic_session_id`)**
- Format and determinism under test mode

> Because the SPAKE2 crate draws its own randomness, the SPAKE2/Noise/SAS byte values vary
> between runs even under test mode; what is reproducible is the *format* and the
> within-run consistency between the two parties (see Known Limitations).

---

## Known Limitations & Mitigations

### 1. SPAKE2 Library Non-Determinism

**Behavior:** the SPAKE2 crate draws randomness via `getrandom`, which test mode cannot seed.

**Effect:**
- SPAKE2 messages differ between test runs even in test mode.
- The Noise handshake hash differs (it depends on SPAKE2 ephemerals).
- SAS values differ across runs.

**Mitigation:**
- Test mode still makes session ids, timestamps, and invite codes deterministic.
- Within a single run both parties derive the same secret, keys, and SAS.

**Severity:** low — affects test reproducibility, not production security.

### 2. Manual Handshake Exchange

**Behavior:** handshake messages (SPAKE2, Noise) can be surfaced as hex strings for manual
out-of-band exchange (QR codes, clipboard) in addition to automatic exchange over the
transport.

**Effect:**
- Manual flows depend on the operator copying the right message in the right order.

**Mitigation:**
- Supports air-gapped wallet use.
- Clear errors on malformed handshake messages.
- SAS verification detects a MITM regardless of exchange channel.

**Severity:** low — intentional design backed by SAS verification.

### 3. No Mid-Handshake Recovery

**Behavior:** if the bridge stops during the handshake, that handshake cannot be resumed.

**Effect:**
- The operators restart from a fresh invite code.

**Mitigation:**
- Established (post-handshake) sessions are persisted and recoverable.
- The handshake itself is fast and cheap to repeat.

**Severity:** low — usability, not security.

### 4. Invite-Code Entropy

**Behavior:** the invite code (the SPAKE2 password) is generated from a 32-word list at ~25
bits of entropy, not the 55-bit EFF-wordlist space used by the SAS.

**Effect:**
- It bounds offline-guessing resistance of the channel password.

**Mitigation:**
- Invite codes are intended to be single-use and short-lived, limiting online guessing.

**Severity:** evaluate against the deployment threat model; see the Invite Code Generation
auditor note. Adopting the full EFF wordlist here would raise it to ~55 bits.

---

## Audit Checklist

### Code Review

**Cryptographic implementation:**
- [ ] SPAKE2 usage — `spake2_start` (`src/crypto/mod.rs:4000`), `spake2_finish`
      (`src/crypto/mod.rs:4037`); identity labels match on both sides.
- [ ] HKDF derivation — `derive_noise_psk` (`src/crypto/mod.rs:3917`).
- [ ] Noise initialization — `init_noise` (`src/crypto/mod.rs:4068`); confirm `NNpsk0`,
      not the dead `Noise_XX` constant at `src/crypto/mod.rs:17`.
- [ ] Noise handshake — `noise_handshake_step` (`src/crypto/mod.rs:4109`),
      `noise_handshake_write` (`src/crypto/mod.rs:4149`); handshake-hash capture on both paths.
- [ ] SAS generation — `generate_sas` (`src/crypto/mod.rs:4241`),
      `derive_sas_from_transcript` (`src/crypto/mod.rs:3927`).
- [ ] Encryption/decryption — `encrypt` (`src/crypto/mod.rs:4193`),
      `decrypt` (`src/crypto/mod.rs:4217`); reject pre-transport calls.

**Test mode:**
- [ ] Test-mode implementation — `src/crypto/test_mode.rs`.
- [ ] Production wrappers — `random_bytes` (`:87`), `current_timestamp_ms` (`:102`).
- [ ] Explicit activation and warning.

**Session management:**
- [ ] Session-id generation — `generate_session_id` (`src/session.rs:2141`).
- [ ] Invite-code generation and entropy — `generate_invite_code` (`src/session.rs:2161`).
- [ ] Rate limiting.
- [ ] Bandwidth limiting.
- [ ] Session persistence and cleanup.

**Error handling:**
- [ ] Error propagation (`anyhow`) leaks no secrets in messages.
- [ ] Panic-free production paths (no `unwrap()` on attacker-influenced input).
- [ ] Resource cleanup on error.

### Test Vector Validation

```bash
cargo test --test crypto_vectors -- --nocapture > audit_vectors.txt
```

- [ ] SPAKE2 message format (compressed points).
- [ ] HKDF derivation with the correct info string.
- [ ] Noise handshake structure (`NNpsk0`).
- [ ] SAS format (5 words).
- [ ] Encryption overhead (16-byte Poly1305 tag).
- [ ] Full protocol-flow consistency.

### Dynamic Testing

**Full suite** (297 tests: 221 unit + 76 integration):
```bash
cargo test
```
- [ ] All tests pass.
- [ ] Test-mode isolation (no cross-contamination).
- [ ] Production randomness is non-deterministic.

**Fuzzing:**
- [ ] SPAKE2 message parsing.
- [ ] Noise handshake message parsing.
- [ ] Encrypted-message decryption.
- [ ] Session-id parsing.
- [ ] Invite-code parsing.

**Side-channel analysis:**
- [ ] Timing of any SAS comparison.
- [ ] Timing of any invite-code comparison.
- [ ] Memory profiling for secret zeroization.

**Stress testing:**
- [ ] Rate-limit effectiveness under high request rates.
- [ ] Bandwidth-limit effectiveness under large messages.
- [ ] Memory behavior under many concurrent sessions.

---

## Security Audit Report Template

### Executive Summary
- Audit scope
- Key findings
- Overall risk assessment (High/Medium/Low)

### Methodology
- Tools (static analysis, fuzzing, manual review)
- Test environment
- Duration

### Findings

For each finding:

- **ID:** e.g. `COSIGN-001`
- **Title:** brief description
- **Severity:** Critical / High / Medium / Low / Informational
- **Component:** e.g. `src/crypto/mod.rs:4241`
- **Description:** detail
- **Impact:** security impact
- **Recommendation:** suggested fix
- **Status:** Open / Fixed / Accepted Risk

### Test Results
- Test-vector validation
- Fuzzing results (crashes, hangs, errors)
- Side-channel results

### Recommendations
- Prioritized fixes and improvements

### Conclusion
- Overall assessment and any follow-up requirements

---

## Contact Information

**Security team:** security@tensorcash.org
**Repository:** https://github.com/tensorcash/tensorcash

Please report security issues privately to security@tensorcash.org. Do **not** open public
GitHub issues for security vulnerabilities.

---

## Appendix A: Dependency Audit

### Cryptographic Libraries
- `spake2` — SPAKE2 PAKE
- `snow` — Noise Protocol Framework
- `hkdf` — HKDF key derivation
- `sha2` — SHA-256
- `rand` / `rand_chacha` — random number generation (`OsRng` in production, `ChaCha20Rng`
  in test mode)

### Tools
```bash
# Known vulnerabilities
cargo audit

# Review the dependency tree for supply-chain risk
cargo tree
```

- [ ] Check crates.io advisories for each crypto dependency.
- [ ] Review the dependency tree for supply-chain risk.
- [ ] Confirm no deprecated or unmaintained dependencies.

---

## Appendix B: Build & Test Environment

**Rust toolchain:** 1.85 (matches CI).
**OS:** Linux (Ubuntu 22.04) or macOS.

**Build & test:**
```bash
# Release build
cargo build --release

# Run the full suite
cargo test

# Crypto test vectors with output
cargo test --test crypto_vectors -- --nocapture
```

**Containerized build/test** (reproducible):
```bash
# from a checkout, run inside a rust:1.85 image with the repo mounted, e.g.
#   docker run --rm -v "$PWD":/workspace -w /workspace/services/core-node/cosign-bridge \
#     rust:1.85 cargo test
```

The cosign-bridge CI job (`.github/workflows/ci.yml:461-587`) installs the Rust 1.85
toolchain (with `clippy` and `rustfmt`) and runs fmt, clippy, the test suite, and a release
build.
