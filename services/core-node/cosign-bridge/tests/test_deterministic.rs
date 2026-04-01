// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Integration tests for deterministic test mode
//!
//! These tests verify that enabling test mode produces reproducible results
//! across multiple runs with the same seed.

use cosign_bridge::crypto::test_mode;
use cosign_bridge::crypto::CryptoSession;
use std::sync::Mutex;

// Global lock to serialize test mode tests (prevents parallel test interference)
static TEST_LOCK: Mutex<()> = Mutex::new(());

#[test]
fn test_spake2_exchange_works_in_test_mode() {
    let _lock = TEST_LOCK.lock().unwrap();
    // Note: SPAKE2 library uses its own internal RNG that we cannot control.
    // This test verifies that SPAKE2 still functions correctly when test mode is enabled,
    // but we cannot expect deterministic outputs from SPAKE2 itself.

    let seed = [0x42; 32];
    let password = "test-password";

    test_mode::TestMode::enable(seed, None);

    let mut init = CryptoSession::new(password).unwrap();
    let mut resp = CryptoSession::new(password).unwrap();

    // SPAKE2 exchange should work (but outputs will vary due to library's internal RNG)
    let init_msg = init.spake2_start(true).unwrap();
    let resp_msg = resp.spake2_start(false).unwrap();

    let init_secret = init.spake2_finish(true, &resp_msg).unwrap();
    let resp_secret = resp.spake2_finish(false, &init_msg).unwrap();

    // Verify that both parties derive the same shared secret (this always works)
    assert_eq!(init_secret, resp_secret, "Shared secrets should match");
    assert_eq!(init_secret.len(), 32, "Shared secret should be 32 bytes");

    test_mode::TestMode::disable();
}

#[test]
fn test_deterministic_session_data() {
    let _lock = TEST_LOCK.lock().unwrap();
    // Test that session-level data (IDs, invite codes) are deterministic
    // Note: We test this indirectly through the crypto_vectors tests

    let seed = [0x42; 32];
    let fixed_time = 1609459200000u64;

    test_mode::TestMode::enable(seed, Some(fixed_time));

    // Session ID generation uses test mode timestamp and randomness
    let ts1 = test_mode::current_timestamp_ms();
    let random1 = test_mode::random_bytes(4);

    test_mode::TestMode::disable();

    // Reset with same seed
    test_mode::TestMode::enable(seed, Some(fixed_time));

    let ts2 = test_mode::current_timestamp_ms();
    let random2 = test_mode::random_bytes(4);

    test_mode::TestMode::disable();

    // Should be deterministic
    assert_eq!(ts1, ts2, "Timestamps should be deterministic");
    assert_eq!(random1, random2, "Random bytes should be deterministic");
}

#[test]
fn test_deterministic_timestamps() {
    let _lock = TEST_LOCK.lock().unwrap();
    let seed = [0x42; 32];
    let fixed_time = 1609459200000u64; // 2021-01-01 00:00:00 UTC

    // Enable test mode with fixed timestamp
    test_mode::TestMode::enable(seed, Some(fixed_time));

    let ts1 = test_mode::current_timestamp_ms();
    let ts2 = test_mode::current_timestamp_ms();

    test_mode::TestMode::disable();

    // Fixed timestamp should always return same value
    assert_eq!(ts1, fixed_time);
    assert_eq!(ts2, fixed_time);
}

#[test]
fn test_deterministic_random_bytes() {
    let _lock = TEST_LOCK.lock().unwrap();
    let seed = [0x42; 32];

    // Run 1
    test_mode::TestMode::enable(seed, None);

    let bytes1_1 = test_mode::random_bytes(32);
    let bytes1_2 = test_mode::random_bytes(32);

    // bytes1_1 and bytes1_2 should be different (RNG progresses)
    assert_ne!(bytes1_1, bytes1_2);

    test_mode::TestMode::disable();

    // Run 2 with same seed
    test_mode::TestMode::enable(seed, None);

    let bytes2_1 = test_mode::random_bytes(32);
    let bytes2_2 = test_mode::random_bytes(32);

    test_mode::TestMode::disable();

    // First call should match between runs
    assert_eq!(
        bytes1_1, bytes2_1,
        "First random_bytes call should be deterministic"
    );
    // Second call should match between runs
    assert_eq!(
        bytes1_2, bytes2_2,
        "Second random_bytes call should be deterministic"
    );
}

#[test]
fn test_production_mode_not_deterministic() {
    let _lock = TEST_LOCK.lock().unwrap();
    test_mode::TestMode::disable();

    // In production mode (no test mode), results should be non-deterministic
    let bytes1 = test_mode::random_bytes(32);
    let bytes2 = test_mode::random_bytes(32);

    // Should be different (very unlikely to match with cryptographically secure RNG)
    assert_ne!(
        bytes1, bytes2,
        "Production mode should produce non-deterministic results"
    );
}

#[test]
fn test_sas_consistency_within_session() {
    let _lock = TEST_LOCK.lock().unwrap();
    // Test that SAS is consistent between two parties in the same session
    // Note: We cannot test determinism across runs due to SPAKE2's internal RNG,
    // but we CAN test that both parties derive the same SAS within one run.

    let seed = [0x42; 32];
    let password = "test-password";

    test_mode::TestMode::enable(seed, None);

    let mut init = CryptoSession::new(password).unwrap();
    let mut resp = CryptoSession::new(password).unwrap();

    // Complete SPAKE2
    let init_msg = init.spake2_start(true).unwrap();
    let resp_msg = resp.spake2_start(false).unwrap();

    init.spake2_finish(true, &resp_msg).unwrap();
    resp.spake2_finish(false, &init_msg).unwrap();

    // Complete Noise handshake
    init.init_noise(true).unwrap();
    resp.init_noise(false).unwrap();

    let noise_msg1 = init.noise_handshake_write().unwrap();
    resp.noise_handshake_step(&noise_msg1).unwrap();
    let noise_resp = resp.noise_handshake_write().unwrap();
    init.noise_handshake_step(&noise_resp).unwrap();

    // Both parties should derive the same SAS
    let init_sas = init.generate_sas("test-session");
    let resp_sas = resp.generate_sas("test-session");

    assert_eq!(init_sas, resp_sas, "Both parties must derive the same SAS");

    // Verify SAS format
    let words: Vec<&str> = init_sas.split('-').collect();
    assert_eq!(words.len(), 5, "SAS should have 5 words");

    test_mode::TestMode::disable();
}
