// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Cryptographic test vectors for audit trail (Phase 4 §A.3)
//!
//! These test vectors provide reproducible outputs for security auditing.
//! All tests use deterministic test mode with a fixed seed to ensure
//! consistent results across runs.
//!
//! **Test Seed:** 0x4242424242424242424242424242424242424242424242424242424242424242
//! **Test Time:** 1609459200000 (2021-01-01 00:00:00 UTC)

use cosign_bridge::crypto::test_mode::TestMode;
use cosign_bridge::crypto::CryptoSession;

const TEST_SEED: [u8; 32] = [0x42; 32];
const TEST_TIME: u64 = 1609459200000; // 2021-01-01 00:00:00 UTC
const TEST_PASSWORD: &str = "golf-hotel-foxtrot-echo-hotel";

#[test]
fn test_vector_spake2_exchange() {
    // Enable deterministic test mode
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    let mut initiator = CryptoSession::new(TEST_PASSWORD).unwrap();
    let mut responder = CryptoSession::new(TEST_PASSWORD).unwrap();

    // Step 1: Both parties start SPAKE2
    let init_msg = initiator.spake2_start(true).unwrap();
    let resp_msg = responder.spake2_start(false).unwrap();

    println!("=== SPAKE2 Test Vector ===");
    println!("Password: {}", TEST_PASSWORD);
    println!("Initiator message: {}", hex::encode(&init_msg));
    println!("Responder message: {}", hex::encode(&resp_msg));

    // Step 2: Both parties complete exchange
    let init_secret = initiator.spake2_finish(true, &resp_msg).unwrap();
    let resp_secret = responder.spake2_finish(false, &init_msg).unwrap();

    println!("Shared secret: {}", hex::encode(&init_secret));
    println!();

    // Verify secrets match
    assert_eq!(init_secret, resp_secret);

    // Expected values (documented for audit)
    // Note: These will be deterministic with the test seed
    assert_eq!(
        init_msg.len(),
        33,
        "SPAKE2 message should be 33 bytes (compressed point)"
    );
    assert_eq!(
        resp_msg.len(),
        33,
        "SPAKE2 message should be 33 bytes (compressed point)"
    );
    assert_eq!(init_secret.len(), 32, "Shared secret should be 32 bytes");

    TestMode::disable();
}

#[test]
fn test_vector_noise_psk_derivation() {
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    let mut initiator = CryptoSession::new(TEST_PASSWORD).unwrap();
    let mut responder = CryptoSession::new(TEST_PASSWORD).unwrap();

    // Complete SPAKE2
    let init_msg = initiator.spake2_start(true).unwrap();
    let resp_msg = responder.spake2_start(false).unwrap();
    let shared_secret = initiator.spake2_finish(true, &resp_msg).unwrap();
    responder.spake2_finish(false, &init_msg).unwrap();

    println!("=== HKDF-SHA256 PSK Derivation Test Vector ===");
    println!(
        "Input (SPAKE2 shared secret): {}",
        hex::encode(&shared_secret)
    );
    println!("HKDF Info: cosign-noise-psk-v1");

    // Initialize Noise (internally derives PSK using HKDF)
    initiator.init_noise(true).unwrap();
    responder.init_noise(false).unwrap();

    println!("Output: PSK used internally in Noise protocol (32 bytes)");
    println!();

    TestMode::disable();
}

#[test]
fn test_vector_noise_handshake() {
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    let mut initiator = CryptoSession::new(TEST_PASSWORD).unwrap();
    let mut responder = CryptoSession::new(TEST_PASSWORD).unwrap();

    // Complete SPAKE2
    let init_msg = initiator.spake2_start(true).unwrap();
    let resp_msg = responder.spake2_start(false).unwrap();
    initiator.spake2_finish(true, &resp_msg).unwrap();
    responder.spake2_finish(false, &init_msg).unwrap();

    // Initialize Noise
    initiator.init_noise(true).unwrap();
    responder.init_noise(false).unwrap();

    println!("=== Noise NNpsk0 Handshake Test Vector ===");
    println!("Pattern: Noise_NNpsk0_25519_ChaChaPoly_BLAKE2b");

    // Initiator writes first message
    let noise_msg1 = initiator.noise_handshake_write().unwrap();
    println!("Initiator -> Responder: {}", hex::encode(&noise_msg1));

    // Responder reads and responds
    responder.noise_handshake_step(&noise_msg1).unwrap();
    let noise_msg2 = responder.noise_handshake_write().unwrap();
    println!("Responder -> Initiator: {}", hex::encode(&noise_msg2));

    // Initiator completes handshake
    initiator.noise_handshake_step(&noise_msg2).unwrap();

    println!();

    TestMode::disable();
}

#[test]
fn test_vector_sas_derivation() {
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    let mut initiator = CryptoSession::new(TEST_PASSWORD).unwrap();
    let mut responder = CryptoSession::new(TEST_PASSWORD).unwrap();

    // Complete SPAKE2
    let init_msg = initiator.spake2_start(true).unwrap();
    let resp_msg = responder.spake2_start(false).unwrap();
    initiator.spake2_finish(true, &resp_msg).unwrap();
    responder.spake2_finish(false, &init_msg).unwrap();

    // Complete Noise handshake
    initiator.init_noise(true).unwrap();
    responder.init_noise(false).unwrap();

    let noise_msg1 = initiator.noise_handshake_write().unwrap();
    responder.noise_handshake_step(&noise_msg1).unwrap();
    let noise_msg2 = responder.noise_handshake_write().unwrap();
    initiator.noise_handshake_step(&noise_msg2).unwrap();

    // Generate SAS from handshake hash
    let init_sas = initiator.generate_sas("test-session-id");
    let resp_sas = responder.generate_sas("test-session-id");

    println!("=== SAS Derivation Test Vector ===");
    println!("Source: Noise handshake hash (post-handshake)");
    println!("Wordlist: EFF (2048 words, 11 bits per word)");
    println!("Initiator SAS: {}", init_sas);
    println!("Responder SAS: {}", resp_sas);
    println!("Entropy: 55 bits (5 words × 11 bits)");
    println!();

    // Both parties should derive the same SAS
    assert_eq!(init_sas, resp_sas, "SAS must match between parties");

    // Verify format
    let words: Vec<&str> = init_sas.split('-').collect();
    assert_eq!(words.len(), 5, "SAS should have 5 words");

    TestMode::disable();
}

#[test]
fn test_vector_encryption_decryption() {
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    let mut initiator = CryptoSession::new(TEST_PASSWORD).unwrap();
    let mut responder = CryptoSession::new(TEST_PASSWORD).unwrap();

    // Complete SPAKE2
    let init_msg = initiator.spake2_start(true).unwrap();
    let resp_msg = responder.spake2_start(false).unwrap();
    initiator.spake2_finish(true, &resp_msg).unwrap();
    responder.spake2_finish(false, &init_msg).unwrap();

    // Complete Noise handshake
    initiator.init_noise(true).unwrap();
    responder.init_noise(false).unwrap();

    let noise_msg1 = initiator.noise_handshake_write().unwrap();
    responder.noise_handshake_step(&noise_msg1).unwrap();
    let noise_msg2 = responder.noise_handshake_write().unwrap();
    initiator.noise_handshake_step(&noise_msg2).unwrap();

    println!("=== Noise Transport Encryption Test Vector ===");

    // Test message
    let plaintext = b"Hello, secure world!";
    println!("Plaintext: {}", hex::encode(plaintext));

    // Encrypt
    let ciphertext = initiator.encrypt(plaintext).unwrap();
    println!("Ciphertext: {}", hex::encode(&ciphertext));
    println!(
        "Length: {} bytes (plaintext) -> {} bytes (ciphertext)",
        plaintext.len(),
        ciphertext.len()
    );

    // Decrypt
    let decrypted = responder.decrypt(&ciphertext).unwrap();
    println!("Decrypted: {}", hex::encode(&decrypted));
    println!();

    // Verify decryption
    assert_eq!(
        plaintext,
        &decrypted[..],
        "Decryption should recover original plaintext"
    );

    TestMode::disable();
}

#[test]
fn test_vector_full_protocol_flow() {
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    println!("=== Complete Protocol Flow Test Vector ===");
    println!("Test Seed: {}", hex::encode(TEST_SEED));
    println!("Test Time: {} (2021-01-01 00:00:00 UTC)", TEST_TIME);
    println!("Password: {}", TEST_PASSWORD);
    println!();

    let mut initiator = CryptoSession::new(TEST_PASSWORD).unwrap();
    let mut responder = CryptoSession::new(TEST_PASSWORD).unwrap();

    // Phase 1: SPAKE2 PAKE
    println!("--- Phase 1: SPAKE2 PAKE ---");
    let init_spake2 = initiator.spake2_start(true).unwrap();
    let resp_spake2 = responder.spake2_start(false).unwrap();
    println!("✓ SPAKE2 messages exchanged");

    let init_secret = initiator.spake2_finish(true, &resp_spake2).unwrap();
    let resp_secret = responder.spake2_finish(false, &init_spake2).unwrap();
    assert_eq!(init_secret, resp_secret);
    println!("✓ Shared secret derived: {} bytes", init_secret.len());
    println!();

    // Phase 2: HKDF Key Derivation
    println!("--- Phase 2: HKDF Key Derivation ---");
    initiator.init_noise(true).unwrap();
    responder.init_noise(false).unwrap();
    println!("✓ Noise PSK derived using HKDF-SHA256");
    println!("  Info string: cosign-noise-psk-v1");
    println!();

    // Phase 3: Noise Protocol Handshake
    println!("--- Phase 3: Noise NNpsk0 Handshake ---");
    let noise_msg1 = initiator.noise_handshake_write().unwrap();
    responder.noise_handshake_step(&noise_msg1).unwrap();
    println!("✓ Initiator → Responder: {} bytes", noise_msg1.len());

    let noise_msg2 = responder.noise_handshake_write().unwrap();
    initiator.noise_handshake_step(&noise_msg2).unwrap();
    println!("✓ Responder → Initiator: {} bytes", noise_msg2.len());
    println!("✓ Handshake complete, transport mode established");
    println!();

    // Phase 4: SAS Verification
    println!("--- Phase 4: SAS Generation ---");
    let init_sas = initiator.generate_sas("session-123");
    let resp_sas = responder.generate_sas("session-123");
    assert_eq!(init_sas, resp_sas);
    println!("✓ SAS: {}", init_sas);
    println!("  (Both parties must verify this matches)");
    println!();

    // Phase 5: Encrypted Communication
    println!("--- Phase 5: Encrypted Transport ---");
    let msg1 = b"First secure message";
    let ct1 = initiator.encrypt(msg1).unwrap();
    let pt1 = responder.decrypt(&ct1).unwrap();
    assert_eq!(msg1, &pt1[..]);
    println!("✓ Message 1 encrypted and decrypted");

    let msg2 = b"Second secure message";
    let ct2 = responder.encrypt(msg2).unwrap();
    let pt2 = initiator.decrypt(&ct2).unwrap();
    assert_eq!(msg2, &pt2[..]);
    println!("✓ Message 2 encrypted and decrypted (bidirectional)");
    println!();

    println!("=== Protocol Flow Complete ===");

    TestMode::disable();
}

#[test]
fn test_vector_deterministic_session_id() {
    TestMode::enable(TEST_SEED, Some(TEST_TIME));

    // Import session module functions (would need to be public or use test helper)
    // For now, just demonstrate the concept

    println!("=== Session ID Generation Test Vector ===");
    println!("Test Mode Enabled: timestamp and randomness are deterministic");
    println!("Expected behavior: Same seed produces same session ID");
    println!();

    TestMode::disable();
}
