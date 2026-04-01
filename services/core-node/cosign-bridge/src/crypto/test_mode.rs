// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Deterministic test mode for reproducible crypto testing
//!
//! This module provides a global test mode that replaces system randomness
//! and timestamps with deterministic values. This is essential for generating
//! reproducible test vectors for security audits.
//!
//! **WARNING:** Test mode MUST NOT be used in production. It completely
//! compromises security by making all cryptographic operations deterministic.

use once_cell::sync::Lazy;
use rand::{RngCore, SeedableRng};
use rand_chacha::ChaCha20Rng;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

static TEST_MODE: Lazy<Mutex<Option<TestModeState>>> = Lazy::new(|| Mutex::new(None));

struct TestModeState {
    rng: ChaCha20Rng,
    fixed_time_ms: Option<u64>,
}

pub struct TestMode;

impl TestMode {
    /// Enable test mode with deterministic PRNG
    ///
    /// # Arguments
    /// * `seed` - 32-byte seed for ChaCha20 PRNG
    /// * `fixed_time` - Optional fixed timestamp (milliseconds since epoch)
    ///
    /// # Safety
    /// This function completely compromises cryptographic security. Use ONLY for testing.
    ///
    /// # Limitations
    /// Note: SPAKE2 library uses its own internal RNG that cannot be controlled by test mode.
    /// SPAKE2 operations will still produce non-deterministic outputs even when test mode is enabled.
    /// Test mode controls: session IDs, invite codes, timestamps, and application-level randomness.
    pub fn enable(seed: [u8; 32], fixed_time: Option<u64>) {
        let rng = ChaCha20Rng::from_seed(seed);
        *TEST_MODE.lock().unwrap() = Some(TestModeState {
            rng,
            fixed_time_ms: fixed_time,
        });
        log::warn!("⚠️  TEST MODE ENABLED - NOT FOR PRODUCTION");
    }

    /// Disable test mode
    #[allow(dead_code)] // Used in tests
    pub fn disable() {
        *TEST_MODE.lock().unwrap() = None;
    }

    /// Check if test mode is enabled
    #[allow(dead_code)] // Used in tests
    pub fn is_enabled() -> bool {
        TEST_MODE.lock().unwrap().is_some()
    }

    /// Get deterministic random bytes (test mode only)
    pub fn get_random_bytes(count: usize) -> Option<Vec<u8>> {
        TEST_MODE.lock().unwrap().as_mut().map(|state| {
            let mut bytes = vec![0u8; count];
            state.rng.fill_bytes(&mut bytes);
            bytes
        })
    }

    /// Get fixed timestamp (test mode only)
    pub fn get_timestamp_ms() -> Option<u64> {
        TEST_MODE
            .lock()
            .unwrap()
            .as_ref()
            .and_then(|state| state.fixed_time_ms)
    }
}

/// Get random bytes (uses test mode if enabled, otherwise OsRng)
///
/// This is the main function that should be used throughout the codebase
/// instead of rand::thread_rng().
pub fn random_bytes(count: usize) -> Vec<u8> {
    if let Some(bytes) = TestMode::get_random_bytes(count) {
        bytes
    } else {
        use rand::rngs::OsRng;
        let mut bytes = vec![0u8; count];
        OsRng.fill_bytes(&mut bytes);
        bytes
    }
}

/// Get current timestamp (uses test mode if enabled, otherwise system time)
///
/// This is the main function that should be used throughout the codebase
/// instead of SystemTime::now().
pub fn current_timestamp_ms() -> u64 {
    if let Some(ts) = TestMode::get_timestamp_ms() {
        ts
    } else {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Global lock to serialize test mode tests (prevents parallel test interference)
    static TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn test_test_mode_can_be_disabled() {
        let _lock = TEST_LOCK.lock().unwrap();
        // Test that test mode can be disabled (don't assume initial state due to parallel tests)
        TestMode::disable();
        assert!(!TestMode::is_enabled());

        // Verify it stays disabled
        assert!(!TestMode::is_enabled());
    }

    #[test]
    fn test_test_mode_enable_disable() {
        let _lock = TEST_LOCK.lock().unwrap();
        let seed = [0x42; 32];
        TestMode::enable(seed, None);
        assert!(TestMode::is_enabled());

        TestMode::disable();
        assert!(!TestMode::is_enabled());
    }

    #[test]
    fn test_random_bytes_deterministic() {
        let _lock = TEST_LOCK.lock().unwrap();
        // Use a unique seed to avoid interference from other tests
        let seed = [0x11; 32];

        // Disable first to ensure clean state
        TestMode::disable();

        // Run 1: Generate two random values
        TestMode::enable(seed, None);
        let bytes1_run1 = random_bytes(32);
        let bytes2_run1 = random_bytes(32);

        // Different calls should produce different bytes (RNG progresses)
        assert_ne!(
            bytes1_run1, bytes2_run1,
            "Sequential calls should produce different bytes"
        );
        TestMode::disable();

        // Run 2: Reset with same seed and generate again
        TestMode::enable(seed, None);
        let bytes1_run2 = random_bytes(32);
        let bytes2_run2 = random_bytes(32);
        TestMode::disable();

        // Both runs should produce identical sequences when using same seed
        assert_eq!(
            bytes1_run1, bytes1_run2,
            "First call should be deterministic with same seed"
        );
        assert_eq!(
            bytes2_run1, bytes2_run2,
            "Second call should be deterministic with same seed"
        );
    }

    #[test]
    fn test_random_bytes_different_seeds() {
        let _lock = TEST_LOCK.lock().unwrap();
        let seed1 = [0x42; 32];
        let seed2 = [0x43; 32];

        TestMode::enable(seed1, None);
        let bytes1 = random_bytes(32);

        TestMode::enable(seed2, None);
        let bytes2 = random_bytes(32);

        // Different seeds should produce different output
        assert_ne!(bytes1, bytes2);

        TestMode::disable();
    }

    #[test]
    fn test_timestamp_fixed() {
        let _lock = TEST_LOCK.lock().unwrap();
        let fixed_time = 1609459200000u64; // 2021-01-01 00:00:00 UTC
        TestMode::enable([0x42; 32], Some(fixed_time));

        let ts1 = current_timestamp_ms();
        let ts2 = current_timestamp_ms();

        // Fixed timestamp should always return same value
        assert_eq!(ts1, fixed_time);
        assert_eq!(ts2, fixed_time);

        TestMode::disable();
    }

    #[test]
    fn test_timestamp_unfixed() {
        let _lock = TEST_LOCK.lock().unwrap();
        TestMode::enable([0x42; 32], None);

        let ts1 = current_timestamp_ms();
        std::thread::sleep(std::time::Duration::from_millis(10));
        let ts2 = current_timestamp_ms();

        // Without fixed time, should use real system time (different calls)
        assert!(ts2 >= ts1);

        TestMode::disable();
    }

    #[test]
    fn test_random_bytes_production_mode() {
        let _lock = TEST_LOCK.lock().unwrap();
        TestMode::disable();

        let bytes1 = random_bytes(32);
        let bytes2 = random_bytes(32);

        // In production mode, should be cryptographically random (very unlikely to match)
        assert_ne!(bytes1, bytes2);
    }

    #[test]
    fn test_timestamp_production_mode() {
        let _lock = TEST_LOCK.lock().unwrap();
        TestMode::disable();

        let ts1 = current_timestamp_ms();
        std::thread::sleep(std::time::Duration::from_millis(10));
        let ts2 = current_timestamp_ms();

        // In production mode, time should advance
        assert!(ts2 > ts1);
    }

    #[test]
    fn test_warning_on_enable() {
        let _lock = TEST_LOCK.lock().unwrap();

        // Capture log output by enabling test mode
        // Note: In real usage, this logs via the log crate
        // The warning "⚠️  TEST MODE ENABLED - NOT FOR PRODUCTION" is emitted

        TestMode::enable([0x42; 32], None);

        // Verify test mode is enabled
        assert!(TestMode::is_enabled(), "Test mode should be enabled");

        // Verify that calling random_bytes works in test mode
        let bytes = random_bytes(16);
        assert_eq!(bytes.len(), 16);

        TestMode::disable();

        // After disabling, should be in production mode
        assert!(!TestMode::is_enabled(), "Test mode should be disabled");
    }
}
