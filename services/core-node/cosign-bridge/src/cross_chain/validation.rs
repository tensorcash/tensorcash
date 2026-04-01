// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! External address validation for BTC, ETH, and TRON.
//!
//! Validation happens before offer posting, offer acceptance, or swap start.
//! This module performs structural / checksum validation only — it does not
//! query any chain backend.

use super::types::ExternalChain;

/// Validate an external address for the given chain.
///
/// This performs **structural validation only**: character set, length,
/// prefix, and checksum where computable locally (EIP-55 for ETH).
///
/// Full network-aware decode, bech32 checksum verification, and
/// base58check hash verification are deferred to the chain adapter
/// at swap time. Adapters **must** re-validate with full checksums
/// before constructing any funding or payout transaction.
///
/// Returns `Ok(())` if the address is structurally valid, or an error
/// string describing why it is not.
pub fn validate_external_address(chain: &ExternalChain, address: &str) -> Result<(), String> {
    match chain {
        ExternalChain::Btc => validate_btc_address(address),
        ExternalChain::Ethereum => validate_eth_address(address),
        ExternalChain::Tron => validate_tron_address(address),
    }
}

// ---------------------------------------------------------------------------
// BTC
// ---------------------------------------------------------------------------

/// Validate a Bitcoin address (mainnet or testnet).
///
/// Supports:
/// - Bech32 / Bech32m (bc1... / tb1... / bcrt1...)
/// - Base58Check (1..., 3..., m/n..., 2...)
///
/// Does NOT resolve names, and does NOT validate against a specific network
/// version here — network-aware decode is left to the adapter at swap time.
fn validate_btc_address(address: &str) -> Result<(), String> {
    if address.is_empty() {
        return Err("BTC address must not be empty".to_string());
    }

    // Bech32 / Bech32m
    let lower = address.to_lowercase();
    if lower.starts_with("bc1") || lower.starts_with("tb1") || lower.starts_with("bcrt1") {
        return validate_bech32(address);
    }

    // Base58Check legacy / P2SH
    validate_base58check(address)
}

/// Minimal Bech32 structural validation.
///
/// Full decode with checksum is left to the adapter's chain backend.
/// Here we check HRP, separator, length, and character set.
fn validate_bech32(address: &str) -> Result<(), String> {
    let lower = address.to_lowercase();

    // Find the last '1' separator
    let sep_pos = lower
        .rfind('1')
        .ok_or_else(|| "bech32: missing separator '1'".to_string())?;

    let hrp = &lower[..sep_pos];
    let data = &lower[sep_pos + 1..];

    // HRP must be bc, tb, or bcrt
    if hrp != "bc" && hrp != "tb" && hrp != "bcrt" {
        return Err(format!("bech32: unsupported HRP '{}'", hrp));
    }

    // Data part must be 6..90 chars (checksum is 6 chars minimum)
    if data.len() < 6 || address.len() > 90 {
        return Err("bech32: invalid length".to_string());
    }

    // Character set: qpzry9x8gf2tvdw0s3jn54khce6mua7l
    const BECH32_CHARSET: &str = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
    for c in data.chars() {
        if !BECH32_CHARSET.contains(c) {
            return Err(format!("bech32: invalid character '{}'", c));
        }
    }

    Ok(())
}

/// Minimal Base58Check structural validation.
fn validate_base58check(address: &str) -> Result<(), String> {
    // Length: 25-34 characters for standard addresses
    if address.len() < 25 || address.len() > 34 {
        return Err(format!(
            "base58check: invalid length {} (expected 25-34)",
            address.len()
        ));
    }

    // Character set: 123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz
    const BASE58_CHARS: &str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
    for c in address.chars() {
        if !BASE58_CHARS.contains(c) {
            return Err(format!("base58check: invalid character '{}'", c));
        }
    }

    // Must start with valid version prefix
    let first = address.chars().next().unwrap();
    // 1 = P2PKH mainnet, 3 = P2SH mainnet, m/n = P2PKH testnet, 2 = P2SH testnet
    if !matches!(first, '1' | '3' | 'm' | 'n' | '2') {
        return Err(format!(
            "base58check: unexpected version prefix '{}'",
            first
        ));
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// ETH
// ---------------------------------------------------------------------------

/// Validate an Ethereum address.
///
/// Accepts 20-byte hex with 0x prefix. Enforces EIP-55 checksum
/// on mixed-case input. No ENS resolution in v1.
fn validate_eth_address(address: &str) -> Result<(), String> {
    if !address.starts_with("0x") && !address.starts_with("0X") {
        return Err("ETH address must start with 0x".to_string());
    }

    let hex_part = &address[2..];
    if hex_part.len() != 40 {
        return Err(format!(
            "ETH address hex part must be 40 characters, got {}",
            hex_part.len()
        ));
    }

    // All chars must be hex
    if !hex_part.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("ETH address contains non-hex characters".to_string());
    }

    // EIP-55 checksum validation on mixed-case input
    let is_all_lower = hex_part.chars().all(|c| !c.is_ascii_uppercase());
    let is_all_upper = hex_part.chars().all(|c| !c.is_ascii_lowercase());
    if !is_all_lower && !is_all_upper {
        validate_eip55_checksum(hex_part)?;
    }

    Ok(())
}

/// Validate EIP-55 mixed-case checksum.
///
/// Uses keccak256 of the lowercase hex to determine expected casing.
fn validate_eip55_checksum(hex_part: &str) -> Result<(), String> {
    let lower_hex = hex_part.to_lowercase();

    // Compute keccak256 of the lowercase hex string
    let hash = keccak256(lower_hex.as_bytes());
    let hash_hex = hex::encode(hash);

    for (i, c) in hex_part.chars().enumerate() {
        if c.is_ascii_digit() {
            continue;
        }
        let hash_nibble = u8::from_str_radix(&hash_hex[i..i + 1], 16).unwrap_or(0);
        let should_be_upper = hash_nibble >= 8;
        if should_be_upper && c.is_ascii_lowercase() {
            return Err(format!("EIP-55 checksum mismatch at position {}", i));
        }
        if !should_be_upper && c.is_ascii_uppercase() {
            return Err(format!("EIP-55 checksum mismatch at position {}", i));
        }
    }

    Ok(())
}

/// Keccak-256 hash (NOT SHA3-256 — Ethereum uses pre-NIST padding).
///
/// Used for EIP-55 checksum validation.
fn keccak256(data: &[u8]) -> [u8; 32] {
    use tiny_keccak::{Hasher, Keccak};
    let mut hasher = Keccak::v256();
    hasher.update(data);
    let mut out = [0u8; 32];
    hasher.finalize(&mut out);
    out
}

// ---------------------------------------------------------------------------
// TRON
// ---------------------------------------------------------------------------

/// Validate a TRON address.
///
/// Accepts base58check form (T-prefix for mainnet) or hex form (41-prefix).
/// No name resolution in v1.
fn validate_tron_address(address: &str) -> Result<(), String> {
    if address.is_empty() {
        return Err("TRON address must not be empty".to_string());
    }

    // Hex form: 42 hex chars starting with "41" (mainnet)
    if address.starts_with("41") && address.len() == 42 {
        if !address.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err("TRON hex address contains non-hex characters".to_string());
        }
        return Ok(());
    }

    // Base58check form: starts with T, 34 characters
    if address.starts_with('T') {
        if address.len() != 34 {
            return Err(format!(
                "TRON base58 address must be 34 characters, got {}",
                address.len()
            ));
        }
        const BASE58_CHARS: &str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
        for c in address.chars() {
            if !BASE58_CHARS.contains(c) {
                return Err(format!("TRON base58 address: invalid character '{}'", c));
            }
        }
        return Ok(());
    }

    Err("TRON address must start with 'T' (base58) or '41' (hex)".to_string())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- BTC --

    #[test]
    fn test_btc_bech32_valid() {
        // Mainnet P2WPKH
        assert!(validate_btc_address("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4").is_ok());
        // Testnet
        assert!(validate_btc_address("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx").is_ok());
        // Regtest
        assert!(validate_btc_address("bcrt1q6z64a43mjgkcq0ul2zaqusq3spghrlau8hmczl").is_ok());
    }

    #[test]
    fn test_btc_base58_valid() {
        // P2PKH mainnet
        assert!(validate_btc_address("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2").is_ok());
        // P2SH mainnet
        assert!(validate_btc_address("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy").is_ok());
    }

    #[test]
    fn test_btc_empty() {
        assert!(validate_btc_address("").is_err());
    }

    #[test]
    fn test_btc_invalid_chars() {
        assert!(validate_btc_address("bc1invalid!chars").is_err());
    }

    // -- ETH --

    #[test]
    fn test_eth_valid_lowercase() {
        assert!(validate_eth_address("0xd8da6bf26964af9d7eed9e03e53415d37aa96045").is_ok());
    }

    #[test]
    fn test_eth_valid_uppercase() {
        assert!(validate_eth_address("0xD8DA6BF26964AF9D7EED9E03E53415D37AA96045").is_ok());
    }

    #[test]
    fn test_eth_valid_eip55_mixed_case() {
        // EIP-55 correctly checksummed addresses
        // Vitalik's address (well-known EIP-55 example)
        assert!(validate_eth_address("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045").is_ok());
        // Another well-known EIP-55 test vector
        assert!(validate_eth_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed").is_ok());
    }

    #[test]
    fn test_eth_bad_eip55_checksum() {
        // Deliberately wrong casing (flip one letter)
        assert!(validate_eth_address("0xd8Da6BF26964aF9D7eEd9e03E53415D37aA96045").is_err());
    }

    #[test]
    fn test_eth_missing_prefix() {
        assert!(validate_eth_address("d8da6bf26964af9d7eed9e03e53415d37aa96045").is_err());
    }

    #[test]
    fn test_eth_wrong_length() {
        assert!(validate_eth_address("0xd8da6bf269").is_err());
    }

    // -- TRON --

    #[test]
    fn test_tron_base58_valid() {
        // Standard TRON mainnet address (34 chars, T-prefix)
        assert!(validate_tron_address("TJCnKsPa7y5okkXvQAidZBzqx3QyQ6sxMW").is_ok());
    }

    #[test]
    fn test_tron_hex_valid() {
        // 41-prefix hex (42 hex chars)
        let hex_addr = "41".to_string() + &"a".repeat(40);
        assert!(validate_tron_address(&hex_addr).is_ok());
    }

    #[test]
    fn test_tron_invalid_prefix() {
        assert!(validate_tron_address("Xnot_a_tron_address_at_all_really1").is_err());
    }

    #[test]
    fn test_tron_wrong_length() {
        assert!(validate_tron_address("Tshort").is_err());
    }

    // -- dispatch through validate_external_address --

    #[test]
    fn test_dispatch() {
        assert!(validate_external_address(
            &ExternalChain::Btc,
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        )
        .is_ok());
        assert!(validate_external_address(
            &ExternalChain::Ethereum,
            "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
        )
        .is_ok());
        assert!(validate_external_address(
            &ExternalChain::Tron,
            "TJCnKsPa7y5okkXvQAidZBzqx3QyQ6sxMW"
        )
        .is_ok());
    }
}
