// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Cosign Bridge - Secure co-signing coordination for TensorCash
//!
//! HWI-style stdio JSON protocol for communication with Bitcoin Core RPC.
//! Handles SPAKE2 PAKE, Noise protocol encryption, and WebSocket transport.

use clap::Parser;
use std::process;

use cosign_bridge::stdio;
use cosign_bridge::stdio::BridgeError;

/// Cosign Bridge - Secure co-signing coordination bridge
#[derive(Parser)]
#[command(name = "cosign-bridge")]
#[command(about = "Secure co-signing coordination for TensorCash", long_about = None)]
struct Args {
    /// Enable test mode with deterministic crypto
    /// WARNING: Test mode completely compromises security - DO NOT use in production
    #[arg(long)]
    test_mode: bool,

    /// Test seed (64 hex characters = 32 bytes)
    /// Only valid when --test-mode is enabled
    #[arg(long, requires = "test_mode")]
    test_seed: Option<String>,

    /// Fixed timestamp (milliseconds since Unix epoch)
    /// Only valid when --test-mode is enabled
    #[arg(long, requires = "test_mode")]
    test_time: Option<u64>,
}

#[tokio::main]
async fn main() {
    // Parse command-line arguments
    let args = Args::parse();

    // Initialize logging
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    // Enable test mode if requested
    if args.test_mode {
        let seed = if let Some(seed_hex) = args.test_seed {
            // Parse hex string to 32-byte array
            let seed_bytes = hex::decode(&seed_hex).expect("Invalid hex string for test_seed");

            if seed_bytes.len() != 32 {
                eprintln!("Error: test_seed must be exactly 32 bytes (64 hex characters)");
                process::exit(1);
            }

            let mut seed_array = [0u8; 32];
            seed_array.copy_from_slice(&seed_bytes);
            seed_array
        } else {
            // Default test seed
            [0x42; 32]
        };

        cosign_bridge::crypto::test_mode::TestMode::enable(seed, args.test_time);
        log::warn!("⚠️  TEST MODE ENABLED - NOT FOR PRODUCTION USE");

        if let Some(ts) = args.test_time {
            log::info!("Using fixed timestamp: {}", ts);
        }
        log::info!("Using test seed: {}", hex::encode(seed));
    }

    // Run the stdio handler
    if let Err(e) = stdio::run().await {
        log::error!("Bridge error: {}", e);
        process::exit(get_exit_code(&e));
    }
}

/// Map errors to exit codes for supervision/restart logic
fn get_exit_code(error: &anyhow::Error) -> i32 {
    // Check error chain for specific error types
    for cause in error.chain() {
        if let Some(err) = cause.downcast_ref::<BridgeError>() {
            return match err {
                BridgeError::BadConfiguration(_) => 10,
                BridgeError::TransportDown(_) => 11,
                BridgeError::CryptoInitFailed(_) => 12,
                _ => 1,
            };
        }
    }
    1 // General error
}
