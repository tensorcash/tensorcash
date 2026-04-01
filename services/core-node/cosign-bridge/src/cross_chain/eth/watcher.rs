// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! HTLC event watcher and oracle attestation builder.
//!
//! Watches the TensorSwapHTLC contract for Locked/Claimed/Refunded events,
//! tracks confirmation depth, and produces oracle attestations for the
//! wallet to verify locally.
//!
//! The oracle attestation format:
//!   - canonical JSON attestation object
//!   - hashed with SHA256
//!   - signed by the oracle's secp256k1 Schnorr key (plan §Oracle attestation format)

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::htlc::{TOPIC_CLAIMED, TOPIC_LOCKED, TOPIC_REFUNDED};
use super::rpc::{EthRpcClient, LogEntry, LogFilter, RpcError};

/// A detected HTLC event with chain context.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HtlcEvent {
    pub event_type: HtlcEventType,
    pub swap_id: String, // hex, 0x-prefixed
    pub tx_hash: String,
    pub block_number: u64,
    pub block_hash: String,
    pub contract_address: String,
    pub log_index: u64,

    // Locked-specific fields (empty for Claimed/Refunded)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sender: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recipient: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub token_address: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub amount: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secret_hash: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timelock: Option<u64>,

    // Claimed-specific
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secret: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HtlcEventType {
    Locked,
    Claimed,
    Refunded,
}

/// Oracle attestation for a confirmed HTLC lock.
///
/// This is what the wallet verifies locally before advancing the TSC ceremony.
/// Per the plan: "The oracle should only attest to external chain facts."
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OracleAttestation {
    pub version: u8,
    pub swap_id: String,
    pub event_type: HtlcEventType,
    pub tx_hash: String,
    pub block_number: u64,
    pub block_hash: String,
    pub contract_address: String,
    pub token_address: String,
    pub amount: String,
    pub recipient: String,
    pub secret_hash: String,
    pub timelock: u64,
    pub confirmation_depth: u64,
    pub attested_at: u64, // unix timestamp

    /// SHA256 of the canonical JSON (without this field and signature).
    pub attestation_hash: String,

    /// secp256k1 Schnorr signature over attestation_hash.
    /// Hex-encoded, 64 bytes.
    pub signature: String,
}

/// HTLC event watcher configuration.
#[derive(Debug, Clone)]
pub struct WatcherConfig {
    /// HTLC contract address (0x-prefixed, checksummed or lowercase).
    pub contract_address: String,
    /// Minimum confirmations before producing an attestation.
    pub min_confirmations: u64,
    /// How many blocks back to start scanning (from current head).
    pub lookback_blocks: u64,
}

/// Watch for HTLC events in a block range.
///
/// Returns all Locked/Claimed/Refunded events found in [from_block, to_block].
pub async fn scan_htlc_events(
    rpc: &EthRpcClient,
    config: &WatcherConfig,
    from_block: u64,
    to_block: u64,
) -> Result<Vec<HtlcEvent>, RpcError> {
    let filter = LogFilter {
        from_block: Some(format!("0x{:x}", from_block)),
        to_block: Some(format!("0x{:x}", to_block)),
        address: Some(config.contract_address.clone()),
        topics: None, // Get all events from the contract
    };

    let logs = rpc.get_logs(&filter).await?;
    let mut events = Vec::new();

    for log in logs {
        if let Some(event) = parse_htlc_log(&log, &config.contract_address) {
            events.push(event);
        }
    }

    Ok(events)
}

/// Check confirmation depth of a specific transaction.
pub async fn get_confirmation_depth(
    rpc: &EthRpcClient,
    tx_hash: &str,
) -> Result<Option<u64>, RpcError> {
    let receipt = rpc.get_tx_receipt(tx_hash).await?;

    match receipt {
        None => Ok(None), // Not mined yet
        Some(r) => {
            let block_num = r.block_number.as_deref().and_then(|s| {
                let s = s.strip_prefix("0x").unwrap_or(s);
                u64::from_str_radix(s, 16).ok()
            });

            match block_num {
                None => Ok(Some(0)),
                Some(bn) => {
                    let current = rpc.block_number().await?;
                    if current >= bn {
                        Ok(Some(current - bn + 1))
                    } else {
                        Ok(Some(0)) // Reorg?
                    }
                }
            }
        }
    }
}

/// Build an oracle attestation for a confirmed Locked event.
///
/// The attestation is unsigned — the caller must sign it with the oracle key.
pub fn build_unsigned_attestation(
    event: &HtlcEvent,
    confirmation_depth: u64,
    attested_at: u64,
) -> OracleAttestation {
    // Build the attestation without hash and signature
    let mut att = OracleAttestation {
        version: 1,
        swap_id: event.swap_id.clone(),
        event_type: event.event_type,
        tx_hash: event.tx_hash.clone(),
        block_number: event.block_number,
        block_hash: event.block_hash.clone(),
        contract_address: event.contract_address.clone(),
        token_address: event.token_address.clone().unwrap_or_default(),
        amount: event.amount.clone().unwrap_or_default(),
        recipient: event.recipient.clone().unwrap_or_default(),
        secret_hash: event.secret_hash.clone().unwrap_or_default(),
        timelock: event.timelock.unwrap_or(0),
        confirmation_depth,
        attested_at,
        attestation_hash: String::new(),
        signature: String::new(),
    };

    // Compute attestation hash (SHA256 of canonical JSON without hash/signature)
    att.attestation_hash = compute_attestation_hash(&att);
    att
}

/// Compute the SHA256 hash of the attestation's canonical fields.
///
/// Excludes `attestation_hash` and `signature` from the hash input.
pub fn compute_attestation_hash(att: &OracleAttestation) -> String {
    // Canonical JSON: sorted keys, no whitespace
    let canonical = serde_json::json!({
        "version": att.version,
        "swap_id": att.swap_id,
        "event_type": att.event_type,
        "tx_hash": att.tx_hash,
        "block_number": att.block_number,
        "block_hash": att.block_hash,
        "contract_address": att.contract_address,
        "token_address": att.token_address,
        "amount": att.amount,
        "recipient": att.recipient,
        "secret_hash": att.secret_hash,
        "timelock": att.timelock,
        "confirmation_depth": att.confirmation_depth,
        "attested_at": att.attested_at,
    });

    let bytes = serde_json::to_vec(&canonical).expect("serialization");
    let hash = Sha256::digest(&bytes);
    hex::encode(hash)
}

// ---------------------------------------------------------------
// Log parsing
// ---------------------------------------------------------------

/// Parse a single log entry into an HtlcEvent, if it matches a known topic.
fn parse_htlc_log(log: &LogEntry, contract_address: &str) -> Option<HtlcEvent> {
    if log.topics.is_empty() {
        return None;
    }

    let topic0 = decode_hex_to_32(&log.topics[0])?;

    if topic0 == *TOPIC_LOCKED {
        parse_locked_log(log, contract_address)
    } else if topic0 == *TOPIC_CLAIMED {
        parse_claimed_log(log, contract_address)
    } else if topic0 == *TOPIC_REFUNDED {
        parse_refunded_log(log, contract_address)
    } else {
        None
    }
}

/// Parse a Locked event log.
///
/// Locked(bytes32 indexed swapId, address indexed sender,
///        address indexed recipient, address tokenAddress,
///        uint256 amount, bytes32 secretHash, uint256 timelock)
///
/// topics[0] = event signature
/// topics[1] = swapId
/// topics[2] = sender
/// topics[3] = recipient
/// data = abi.encode(tokenAddress, amount, secretHash, timelock)
fn parse_locked_log(log: &LogEntry, contract_address: &str) -> Option<HtlcEvent> {
    if log.topics.len() < 4 {
        return None;
    }

    let data = decode_hex_bytes(&log.data)?;
    if data.len() < 4 * 32 {
        return None;
    }

    let swap_id = log.topics[1].clone();
    let sender = format!("0x{}", &log.topics[2][26..]); // last 20 bytes
    let recipient = format!("0x{}", &log.topics[3][26..]);

    let token_address = format!("0x{}", hex::encode(&data[12..32]));
    let amount = format!("0x{}", hex::encode(&data[32..64]));
    let secret_hash = format!("0x{}", hex::encode(&data[64..96]));
    let timelock = u64_from_be_slice(&data[120..128]);

    Some(HtlcEvent {
        event_type: HtlcEventType::Locked,
        swap_id,
        tx_hash: log.transaction_hash.clone().unwrap_or_default(),
        block_number: parse_hex_block(log.block_number.as_deref()),
        block_hash: String::new(), // Not in log, fetched separately if needed
        contract_address: contract_address.to_string(),
        log_index: parse_hex_block(log.log_index.as_deref()),
        sender: Some(sender),
        recipient: Some(recipient),
        token_address: Some(token_address),
        amount: Some(amount),
        secret_hash: Some(secret_hash),
        timelock: Some(timelock),
        secret: None,
    })
}

/// Parse a Claimed event log.
///
/// Claimed(bytes32 indexed swapId, bytes32 secret, address indexed recipient)
fn parse_claimed_log(log: &LogEntry, contract_address: &str) -> Option<HtlcEvent> {
    if log.topics.len() < 3 {
        return None;
    }

    let data = decode_hex_bytes(&log.data)?;
    if data.len() < 32 {
        return None;
    }

    let swap_id = log.topics[1].clone();
    let recipient = format!("0x{}", &log.topics[2][26..]);
    let secret = format!("0x{}", hex::encode(&data[0..32]));

    Some(HtlcEvent {
        event_type: HtlcEventType::Claimed,
        swap_id,
        tx_hash: log.transaction_hash.clone().unwrap_or_default(),
        block_number: parse_hex_block(log.block_number.as_deref()),
        block_hash: String::new(),
        contract_address: contract_address.to_string(),
        log_index: parse_hex_block(log.log_index.as_deref()),
        sender: None,
        recipient: Some(recipient),
        token_address: None,
        amount: None,
        secret_hash: None,
        timelock: None,
        secret: Some(secret),
    })
}

/// Parse a Refunded event log.
///
/// Refunded(bytes32 indexed swapId, address indexed sender)
fn parse_refunded_log(log: &LogEntry, contract_address: &str) -> Option<HtlcEvent> {
    if log.topics.len() < 3 {
        return None;
    }

    let swap_id = log.topics[1].clone();
    let sender = format!("0x{}", &log.topics[2][26..]);

    Some(HtlcEvent {
        event_type: HtlcEventType::Refunded,
        swap_id,
        tx_hash: log.transaction_hash.clone().unwrap_or_default(),
        block_number: parse_hex_block(log.block_number.as_deref()),
        block_hash: String::new(),
        contract_address: contract_address.to_string(),
        log_index: parse_hex_block(log.log_index.as_deref()),
        sender: Some(sender),
        recipient: None,
        token_address: None,
        amount: None,
        secret_hash: None,
        timelock: None,
        secret: None,
    })
}

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

fn decode_hex_to_32(hex_str: &str) -> Option<[u8; 32]> {
    let s = hex_str.strip_prefix("0x").unwrap_or(hex_str);
    let bytes = hex::decode(s).ok()?;
    if bytes.len() != 32 {
        return None;
    }
    let mut out = [0u8; 32];
    out.copy_from_slice(&bytes);
    Some(out)
}

fn decode_hex_bytes(hex_str: &str) -> Option<Vec<u8>> {
    let s = hex_str.strip_prefix("0x").unwrap_or(hex_str);
    hex::decode(s).ok()
}

fn u64_from_be_slice(data: &[u8]) -> u64 {
    let mut buf = [0u8; 8];
    buf.copy_from_slice(data);
    u64::from_be_bytes(buf)
}

fn parse_hex_block(hex_str: Option<&str>) -> u64 {
    hex_str
        .and_then(|s| {
            let s = s.strip_prefix("0x").unwrap_or(s);
            u64::from_str_radix(s, 16).ok()
        })
        .unwrap_or(0)
}

// ---------------------------------------------------------------
// Oracle attestation verification
// ---------------------------------------------------------------

/// Verify an oracle attestation's integrity and Schnorr signature.
///
/// 1. Re-computes the attestation hash from the canonical fields
/// 2. Checks it matches the stored attestation_hash
/// 3. Verifies the Schnorr signature over the hash using the oracle pubkey
///
/// `oracle_pubkey` is a 32-byte x-only public key (hex-encoded, no 0x prefix).
pub fn verify_attestation(
    oracle_pubkey_hex: &str,
    attestation: &OracleAttestation,
) -> Result<bool, String> {
    // Re-compute the attestation hash
    let computed_hash = compute_attestation_hash(attestation);
    if computed_hash != attestation.attestation_hash {
        return Err(format!(
            "Attestation hash mismatch: computed={}, stored={}",
            computed_hash, attestation.attestation_hash
        ));
    }

    // Parse oracle pubkey (32-byte x-only)
    let pubkey_bytes = hex::decode(
        oracle_pubkey_hex
            .strip_prefix("0x")
            .unwrap_or(oracle_pubkey_hex),
    )
    .map_err(|e| format!("Invalid oracle pubkey hex: {}", e))?;
    if pubkey_bytes.len() != 32 {
        return Err(format!(
            "Oracle pubkey must be 32 bytes, got {}",
            pubkey_bytes.len()
        ));
    }

    // Parse the signature (64 bytes)
    let sig_bytes = hex::decode(
        attestation
            .signature
            .strip_prefix("0x")
            .unwrap_or(&attestation.signature),
    )
    .map_err(|e| format!("Invalid signature hex: {}", e))?;
    if sig_bytes.len() != 64 {
        return Err(format!(
            "Signature must be 64 bytes, got {}",
            sig_bytes.len()
        ));
    }

    // Parse the attestation hash as the message
    let msg_bytes = hex::decode(&attestation.attestation_hash)
        .map_err(|e| format!("Invalid attestation hash hex: {}", e))?;
    if msg_bytes.len() != 32 {
        return Err("Attestation hash must be 32 bytes".to_string());
    }

    // Verify using k256 schnorr
    use k256::schnorr::signature::Verifier;
    use k256::schnorr::{Signature, VerifyingKey};

    let verifying_key = VerifyingKey::from_bytes(pubkey_bytes[..32].try_into().unwrap())
        .map_err(|e| format!("Invalid oracle verifying key: {}", e))?;

    let signature = Signature::try_from(sig_bytes.as_slice())
        .map_err(|e| format!("Invalid Schnorr signature: {}", e))?;

    match verifying_key.verify(&msg_bytes, &signature) {
        Ok(()) => Ok(true),
        Err(e) => Err(format!("Schnorr verification failed: {}", e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_attestation_hash_deterministic() {
        let event = HtlcEvent {
            event_type: HtlcEventType::Locked,
            swap_id: "0xaabb".to_string(),
            tx_hash: "0x1234".to_string(),
            block_number: 100,
            block_hash: "0x5678".to_string(),
            contract_address: "0xcontract".to_string(),
            log_index: 0,
            sender: Some("0xsender".to_string()),
            recipient: Some("0xrecipient".to_string()),
            token_address: Some("0x0000000000000000000000000000000000000000".to_string()),
            amount: Some("1000000000000000000".to_string()),
            secret_hash: Some("0xhash".to_string()),
            timelock: Some(86400),
            secret: None,
        };

        let att1 = build_unsigned_attestation(&event, 12, 1000000);
        let att2 = build_unsigned_attestation(&event, 12, 1000000);

        assert_eq!(att1.attestation_hash, att2.attestation_hash);
        assert!(!att1.attestation_hash.is_empty());
    }

    #[test]
    fn test_attestation_hash_changes_with_depth() {
        let event = HtlcEvent {
            event_type: HtlcEventType::Locked,
            swap_id: "0xaabb".to_string(),
            tx_hash: "0x1234".to_string(),
            block_number: 100,
            block_hash: "0x5678".to_string(),
            contract_address: "0xcontract".to_string(),
            log_index: 0,
            sender: None,
            recipient: None,
            token_address: None,
            amount: None,
            secret_hash: None,
            timelock: None,
            secret: None,
        };

        let att1 = build_unsigned_attestation(&event, 6, 1000000);
        let att2 = build_unsigned_attestation(&event, 12, 1000000);

        assert_ne!(att1.attestation_hash, att2.attestation_hash);
    }
}
