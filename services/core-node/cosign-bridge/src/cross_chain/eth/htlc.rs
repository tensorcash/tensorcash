// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! ABI encoding for TensorSwapHTLC contract calls.
//!
//! Encodes calldata for lock(), lockToken(), claim(), refund(), getSwap()
//! and decodes return data and event logs.
//!
//! Uses raw ABI encoding — no ethers/alloy dependency.

use lazy_static::lazy_static;
use sha2::{Digest, Sha256};
use tiny_keccak::{Hasher, Keccak};

/// HTLC swap state as returned by getSwap().
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HtlcState {
    Empty = 0,
    Locked = 1,
    Claimed = 2,
    Refunded = 3,
}

impl HtlcState {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(Self::Empty),
            1 => Some(Self::Locked),
            2 => Some(Self::Claimed),
            3 => Some(Self::Refunded),
            _ => None,
        }
    }
}

/// Decoded getSwap() result.
#[derive(Debug, Clone)]
pub struct SwapInfo {
    pub state: HtlcState,
    pub sender: [u8; 20],
    pub recipient: [u8; 20],
    pub token_address: [u8; 20],
    pub amount: [u8; 32],
    pub secret_hash: [u8; 32],
    pub timelock: u64,
}

// ---------------------------------------------------------------
// Function selectors (first 4 bytes of keccak256 of signature)
// ---------------------------------------------------------------

/// Compute keccak256 using tiny_keccak (runtime).
fn runtime_keccak256(data: &[u8]) -> [u8; 32] {
    let mut hasher = Keccak::v256();
    hasher.update(data);
    let mut out = [0u8; 32];
    hasher.finalize(&mut out);
    out
}

fn compute_selector(sig: &str) -> [u8; 4] {
    let hash = runtime_keccak256(sig.as_bytes());
    [hash[0], hash[1], hash[2], hash[3]]
}

lazy_static! {
    static ref SEL_LOCK: [u8; 4] = compute_selector("lock(bytes32,address,bytes32,uint256)");
    static ref SEL_LOCK_TOKEN: [u8; 4] = compute_selector("lockToken(bytes32,address,address,uint256,bytes32,uint256)");
    static ref SEL_CLAIM: [u8; 4] = compute_selector("claim(bytes32,bytes32)");
    static ref SEL_REFUND: [u8; 4] = compute_selector("refund(bytes32)");
    static ref SEL_GET_SWAP: [u8; 4] = compute_selector("getSwap(bytes32)");

    /// Locked(bytes32,address,address,address,uint256,bytes32,uint256) event topic
    pub static ref TOPIC_LOCKED: [u8; 32] = runtime_keccak256(
        b"Locked(bytes32,address,address,address,uint256,bytes32,uint256)"
    );
    /// Claimed(bytes32,bytes32,address) event topic
    pub static ref TOPIC_CLAIMED: [u8; 32] = runtime_keccak256(
        b"Claimed(bytes32,bytes32,address)"
    );
    /// Refunded(bytes32,address) event topic
    pub static ref TOPIC_REFUNDED: [u8; 32] = runtime_keccak256(
        b"Refunded(bytes32,address)"
    );
}

// ---------------------------------------------------------------
// Calldata encoders
// ---------------------------------------------------------------

/// Encode calldata for lock(swapId, recipient, secretHash, timelock).
/// The caller must attach ETH value separately.
pub fn encode_lock(
    swap_id: &[u8; 32],
    recipient: &[u8; 20],
    secret_hash: &[u8; 32],
    timelock: u64,
) -> Vec<u8> {
    let mut data = Vec::with_capacity(4 + 4 * 32);
    data.extend_from_slice(&*SEL_LOCK);
    data.extend_from_slice(swap_id);
    data.extend_from_slice(&pad_address(recipient));
    data.extend_from_slice(secret_hash);
    data.extend_from_slice(&pad_u256(timelock));
    data
}

/// Encode calldata for lockToken(swapId, recipient, tokenAddress, amount, secretHash, timelock).
pub fn encode_lock_token(
    swap_id: &[u8; 32],
    recipient: &[u8; 20],
    token_address: &[u8; 20],
    amount: &[u8; 32],
    secret_hash: &[u8; 32],
    timelock: u64,
) -> Vec<u8> {
    let mut data = Vec::with_capacity(4 + 6 * 32);
    data.extend_from_slice(&*SEL_LOCK_TOKEN);
    data.extend_from_slice(swap_id);
    data.extend_from_slice(&pad_address(recipient));
    data.extend_from_slice(&pad_address(token_address));
    data.extend_from_slice(amount);
    data.extend_from_slice(secret_hash);
    data.extend_from_slice(&pad_u256(timelock));
    data
}

/// Encode calldata for claim(swapId, secret).
pub fn encode_claim(swap_id: &[u8; 32], secret: &[u8; 32]) -> Vec<u8> {
    let mut data = Vec::with_capacity(4 + 2 * 32);
    data.extend_from_slice(&*SEL_CLAIM);
    data.extend_from_slice(swap_id);
    data.extend_from_slice(secret);
    data
}

/// Encode calldata for refund(swapId).
pub fn encode_refund(swap_id: &[u8; 32]) -> Vec<u8> {
    let mut data = Vec::with_capacity(4 + 32);
    data.extend_from_slice(&*SEL_REFUND);
    data.extend_from_slice(swap_id);
    data
}

/// Encode calldata for getSwap(swapId).
pub fn encode_get_swap(swap_id: &[u8; 32]) -> Vec<u8> {
    let mut data = Vec::with_capacity(4 + 32);
    data.extend_from_slice(&*SEL_GET_SWAP);
    data.extend_from_slice(swap_id);
    data
}

// ---------------------------------------------------------------
// Return data decoders
// ---------------------------------------------------------------

/// Decode the return data from getSwap().
/// Returns (state, sender, recipient, tokenAddress, amount, secretHash, timelock)
/// ABI: 7 × 32 bytes = 224 bytes.
pub fn decode_get_swap(data: &[u8]) -> Option<SwapInfo> {
    if data.len() < 7 * 32 {
        return None;
    }

    let state = HtlcState::from_u8(data[31])?;
    let sender = extract_address(&data[32..64]);
    let recipient = extract_address(&data[64..96]);
    let token_address = extract_address(&data[96..128]);

    let mut amount = [0u8; 32];
    amount.copy_from_slice(&data[128..160]);

    let mut secret_hash = [0u8; 32];
    secret_hash.copy_from_slice(&data[160..192]);

    let timelock = u64_from_be_bytes(&data[192..224]);

    Some(SwapInfo {
        state,
        sender,
        recipient,
        token_address,
        amount,
        secret_hash,
        timelock,
    })
}

// ---------------------------------------------------------------
// Secret hashing
// ---------------------------------------------------------------

/// Compute sha256(secret) — matches the HTLC contract's hash function.
pub fn compute_secret_hash(secret: &[u8; 32]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(secret);
    let result = hasher.finalize();
    let mut out = [0u8; 32];
    out.copy_from_slice(&result);
    out
}

// ---------------------------------------------------------------
// ABI helpers
// ---------------------------------------------------------------

/// Left-pad a 20-byte address to 32 bytes.
fn pad_address(addr: &[u8; 20]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[12..].copy_from_slice(addr);
    out
}

/// Encode a u64 as a 32-byte big-endian ABI word.
fn pad_u256(val: u64) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[24..].copy_from_slice(&val.to_be_bytes());
    out
}

/// Extract a 20-byte address from a 32-byte ABI word (right-aligned).
fn extract_address(word: &[u8]) -> [u8; 20] {
    let mut out = [0u8; 20];
    out.copy_from_slice(&word[12..32]);
    out
}

/// Extract a u64 from the last 8 bytes of a 32-byte ABI word.
fn u64_from_be_bytes(word: &[u8]) -> u64 {
    let mut buf = [0u8; 8];
    buf.copy_from_slice(&word[24..32]);
    u64::from_be_bytes(buf)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_selector_lock() {
        // Verify selector is deterministic and 4 bytes
        assert_eq!(SEL_LOCK.len(), 4);
        let sel2 = compute_selector("lock(bytes32,address,bytes32,uint256)");
        assert_eq!(*SEL_LOCK, sel2);
    }

    #[test]
    fn test_selector_claim() {
        assert_eq!(SEL_CLAIM.len(), 4);
    }

    #[test]
    fn test_encode_decode_get_swap() {
        let swap_id = [0xABu8; 32];
        let calldata = encode_get_swap(&swap_id);
        assert_eq!(calldata.len(), 4 + 32);
        assert_eq!(&calldata[0..4], &*SEL_GET_SWAP);
        assert_eq!(&calldata[4..36], &swap_id);
    }

    #[test]
    fn test_encode_claim() {
        let swap_id = [1u8; 32];
        let secret = [2u8; 32];
        let calldata = encode_claim(&swap_id, &secret);
        assert_eq!(calldata.len(), 4 + 64);
        assert_eq!(&calldata[4..36], &swap_id);
        assert_eq!(&calldata[36..68], &secret);
    }

    #[test]
    fn test_compute_secret_hash() {
        let secret = [0xDEu8; 32];
        let hash = compute_secret_hash(&secret);
        // Verify it's sha256, not keccak256
        let mut hasher = Sha256::new();
        hasher.update(secret);
        let expected: [u8; 32] = hasher.finalize().into();
        assert_eq!(hash, expected);
    }

    #[test]
    fn test_decode_get_swap_roundtrip() {
        // Construct a fake ABI return: 7 × 32 bytes
        let mut data = vec![0u8; 7 * 32];
        // state = 1 (LOCKED)
        data[31] = 1;
        // sender = 0x11..11
        data[44..64].copy_from_slice(&[0x11u8; 20]);
        // recipient = 0x22..22
        data[76..96].copy_from_slice(&[0x22u8; 20]);
        // token_address = 0x00..00 (native ETH)
        // amount = 1 ETH (10^18)
        data[152..160].copy_from_slice(&1_000_000_000_000_000_000u64.to_be_bytes());
        // secret_hash
        data[160..192].copy_from_slice(&[0xAA; 32]);
        // timelock = 86400
        data[216..224].copy_from_slice(&86400u64.to_be_bytes());

        let info = decode_get_swap(&data).unwrap();
        assert_eq!(info.state, HtlcState::Locked);
        assert_eq!(info.sender, [0x11u8; 20]);
        assert_eq!(info.recipient, [0x22u8; 20]);
        assert_eq!(info.token_address, [0u8; 20]);
        assert_eq!(info.timelock, 86400);
    }

    #[test]
    fn test_runtime_keccak256_empty() {
        // keccak256("") = c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
        let hash = runtime_keccak256(b"");
        let expected =
            hex::decode("c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
                .unwrap();
        assert_eq!(&hash[..], &expected[..]);
    }

    #[test]
    fn test_runtime_keccak256_known_vector() {
        // keccak256("abc") = 4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45
        let hash = runtime_keccak256(b"abc");
        let expected =
            hex::decode("4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
                .unwrap();
        assert_eq!(&hash[..], &expected[..]);
    }
}
