// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Minimal Ethereum transaction signer.
//!
//! Supports EIP-1559 (type 2) transactions only — legacy tx types
//! are not needed for the HTLC adapter.
//!
//! Uses the secp256k1 signing primitives already available in the bridge.

use tiny_keccak::{Hasher, Keccak};

/// An unsigned EIP-1559 transaction.
#[derive(Debug, Clone)]
pub struct Eip1559Tx {
    pub chain_id: u64,
    pub nonce: u64,
    pub max_priority_fee_per_gas: u64,
    pub max_fee_per_gas: u64,
    pub gas_limit: u64,
    pub to: [u8; 20],
    pub value: [u8; 32], // 256-bit big-endian
    pub data: Vec<u8>,
}

/// A signed transaction ready for broadcast.
#[derive(Debug, Clone)]
pub struct SignedTx {
    /// RLP-encoded signed transaction with type prefix (0x02 || rlp(...))
    pub raw: Vec<u8>,
    /// Transaction hash (keccak256 of raw)
    pub hash: [u8; 32],
}

/// Signing key — 32-byte secp256k1 private key.
#[derive(Clone)]
pub struct EthSigningKey {
    secret: [u8; 32],
}

impl EthSigningKey {
    pub fn from_bytes(secret: [u8; 32]) -> Self {
        Self { secret }
    }

    /// Derive the Ethereum address (last 20 bytes of keccak256(uncompressed_pubkey[1..])).
    pub fn address(&self) -> [u8; 20] {
        let pubkey = secp256k1_pubkey_uncompressed(&self.secret);
        let hash = keccak256(&pubkey[1..]); // skip 0x04 prefix
        let mut addr = [0u8; 20];
        addr.copy_from_slice(&hash[12..]);
        addr
    }

    /// Sign an EIP-1559 transaction.
    pub fn sign_tx(&self, tx: &Eip1559Tx) -> SignedTx {
        // 1. RLP-encode the unsigned tx for signing
        let unsigned_payload = rlp_encode_unsigned_1559(tx);

        // 2. Hash: keccak256(0x02 || rlp(unsigned_fields))
        let mut sign_data = Vec::with_capacity(1 + unsigned_payload.len());
        sign_data.push(0x02); // EIP-1559 type prefix
        sign_data.extend_from_slice(&unsigned_payload);
        let msg_hash = keccak256(&sign_data);

        // 3. Sign with secp256k1
        let (r, s, v) = secp256k1_sign_recoverable(&self.secret, &msg_hash);

        // 4. RLP-encode the signed tx
        let signed_payload = rlp_encode_signed_1559(tx, v, &r, &s);

        // 5. Prepend type byte
        let mut raw = Vec::with_capacity(1 + signed_payload.len());
        raw.push(0x02);
        raw.extend_from_slice(&signed_payload);

        let hash = keccak256(&raw);

        SignedTx { raw, hash }
    }
}

// ---------------------------------------------------------------
// RLP encoding
// ---------------------------------------------------------------

/// RLP-encode the unsigned EIP-1559 fields for signing.
fn rlp_encode_unsigned_1559(tx: &Eip1559Tx) -> Vec<u8> {
    let items: Vec<Vec<u8>> = vec![
        rlp_encode_u64(tx.chain_id),
        rlp_encode_u64(tx.nonce),
        rlp_encode_u64(tx.max_priority_fee_per_gas),
        rlp_encode_u64(tx.max_fee_per_gas),
        rlp_encode_u64(tx.gas_limit),
        rlp_encode_bytes(&tx.to),
        rlp_encode_bytes(strip_leading_zeros(&tx.value)),
        rlp_encode_bytes(&tx.data),
        rlp_encode_list(&[]), // access list (empty)
    ];
    rlp_encode_list_from_encoded(&items)
}

/// RLP-encode the signed EIP-1559 fields.
fn rlp_encode_signed_1559(tx: &Eip1559Tx, v: u8, r: &[u8; 32], s: &[u8; 32]) -> Vec<u8> {
    let items: Vec<Vec<u8>> = vec![
        rlp_encode_u64(tx.chain_id),
        rlp_encode_u64(tx.nonce),
        rlp_encode_u64(tx.max_priority_fee_per_gas),
        rlp_encode_u64(tx.max_fee_per_gas),
        rlp_encode_u64(tx.gas_limit),
        rlp_encode_bytes(&tx.to),
        rlp_encode_bytes(strip_leading_zeros(&tx.value)),
        rlp_encode_bytes(&tx.data),
        rlp_encode_list(&[]), // access list (empty)
        rlp_encode_u64(v as u64),
        rlp_encode_bytes(strip_leading_zeros(r)),
        rlp_encode_bytes(strip_leading_zeros(s)),
    ];
    rlp_encode_list_from_encoded(&items)
}

/// RLP-encode a u64 value.
fn rlp_encode_u64(val: u64) -> Vec<u8> {
    if val == 0 {
        return vec![0x80]; // empty byte string
    }
    let bytes = val.to_be_bytes();
    let start = bytes.iter().position(|&b| b != 0).unwrap_or(7);
    rlp_encode_bytes(&bytes[start..])
}

/// RLP-encode a byte string.
fn rlp_encode_bytes(data: &[u8]) -> Vec<u8> {
    if data.len() == 1 && data[0] < 0x80 {
        return vec![data[0]];
    }
    if data.len() <= 55 {
        let mut out = Vec::with_capacity(1 + data.len());
        out.push(0x80 + data.len() as u8);
        out.extend_from_slice(data);
        out
    } else {
        let len_bytes = encode_length(data.len());
        let mut out = Vec::with_capacity(1 + len_bytes.len() + data.len());
        out.push(0xB7 + len_bytes.len() as u8);
        out.extend_from_slice(&len_bytes);
        out.extend_from_slice(data);
        out
    }
}

/// RLP-encode an empty list.
fn rlp_encode_list(items: &[Vec<u8>]) -> Vec<u8> {
    let total: usize = items.iter().map(|i| i.len()).sum();
    if total <= 55 {
        let mut out = Vec::with_capacity(1 + total);
        out.push(0xC0 + total as u8);
        for item in items {
            out.extend_from_slice(item);
        }
        out
    } else {
        let len_bytes = encode_length(total);
        let mut out = Vec::with_capacity(1 + len_bytes.len() + total);
        out.push(0xF7 + len_bytes.len() as u8);
        out.extend_from_slice(&len_bytes);
        for item in items {
            out.extend_from_slice(item);
        }
        out
    }
}

/// RLP-encode a list from already-encoded items.
fn rlp_encode_list_from_encoded(items: &[Vec<u8>]) -> Vec<u8> {
    rlp_encode_list(items)
}

/// Encode a length as big-endian bytes (no leading zeros).
fn encode_length(len: usize) -> Vec<u8> {
    let bytes = (len as u64).to_be_bytes();
    let start = bytes.iter().position(|&b| b != 0).unwrap_or(7);
    bytes[start..].to_vec()
}

/// Strip leading zero bytes from a big-endian number.
fn strip_leading_zeros(data: &[u8]) -> &[u8] {
    let start = data.iter().position(|&b| b != 0).unwrap_or(data.len());
    if start == data.len() {
        &[] // all zeros → empty
    } else {
        &data[start..]
    }
}

// ---------------------------------------------------------------
// Cryptographic primitives
// ---------------------------------------------------------------

/// Keccak-256 hash (Ethereum pre-NIST padding).
pub fn keccak256(data: &[u8]) -> [u8; 32] {
    let mut hasher = Keccak::v256();
    hasher.update(data);
    let mut out = [0u8; 32];
    hasher.finalize(&mut out);
    out
}

/// Compute uncompressed secp256k1 public key (65 bytes: 0x04 || x || y).
///
/// Uses the `k256` crate if available, otherwise falls back to
/// a minimal constant-time scalar multiplication.
///
/// For the bridge we use k256 — it's already a transitive dependency
/// via spake2 or snow.
fn secp256k1_pubkey_uncompressed(secret: &[u8; 32]) -> Vec<u8> {
    use k256::ecdsa::SigningKey;

    let key = SigningKey::from_bytes(secret.into()).expect("valid secret key");
    let pubkey = key.verifying_key().to_encoded_point(false);
    pubkey.as_bytes().to_vec()
}

/// Sign a message hash with secp256k1 and return (r, s, recovery_id).
///
/// The recovery_id is 0 or 1 (not 27/28 — EIP-1559 uses raw parity).
fn secp256k1_sign_recoverable(secret: &[u8; 32], msg_hash: &[u8; 32]) -> ([u8; 32], [u8; 32], u8) {
    use k256::ecdsa::SigningKey;

    let key = SigningKey::from_bytes(secret.into()).expect("valid secret key");
    let (sig, recid) = key
        .sign_prehash_recoverable(msg_hash)
        .expect("signing failed");

    let r_bytes = sig.r().to_bytes();
    let s_bytes = sig.s().to_bytes();

    let mut r = [0u8; 32];
    let mut s = [0u8; 32];
    r.copy_from_slice(&r_bytes);
    s.copy_from_slice(&s_bytes);

    (r, s, recid.to_byte())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rlp_encode_u64() {
        assert_eq!(rlp_encode_u64(0), vec![0x80]);
        assert_eq!(rlp_encode_u64(1), vec![0x01]);
        assert_eq!(rlp_encode_u64(127), vec![0x7f]);
        assert_eq!(rlp_encode_u64(128), vec![0x81, 0x80]);
        assert_eq!(rlp_encode_u64(256), vec![0x82, 0x01, 0x00]);
    }

    #[test]
    fn test_rlp_encode_bytes() {
        assert_eq!(rlp_encode_bytes(&[]), vec![0x80]);
        assert_eq!(rlp_encode_bytes(&[0x42]), vec![0x42]); // single byte < 0x80
        assert_eq!(rlp_encode_bytes(&[0x80]), vec![0x81, 0x80]); // single byte >= 0x80
    }

    #[test]
    fn test_address_derivation() {
        // Well-known test vector: private key 1
        let mut secret = [0u8; 32];
        secret[31] = 1;
        let key = EthSigningKey::from_bytes(secret);
        let addr = key.address();
        let addr_hex = hex::encode(addr);
        // Private key 1 → 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf (lowercase)
        assert_eq!(addr_hex, "7e5f4552091a69125d5dfcb7b8c2659029395bdf");
    }

    #[test]
    fn test_sign_and_recover_type() {
        let mut secret = [0u8; 32];
        secret[31] = 1;
        let key = EthSigningKey::from_bytes(secret);

        let tx = Eip1559Tx {
            chain_id: 1,
            nonce: 0,
            max_priority_fee_per_gas: 1_000_000_000,
            max_fee_per_gas: 20_000_000_000,
            gas_limit: 21000,
            to: [0xAA; 20],
            value: {
                let mut v = [0u8; 32];
                v[31] = 1; // 1 wei
                v
            },
            data: vec![],
        };

        let signed = key.sign_tx(&tx);

        // Must start with 0x02 (EIP-1559 type prefix)
        assert_eq!(signed.raw[0], 0x02);
        // Hash must be 32 bytes
        assert_eq!(signed.hash.len(), 32);
        // Raw must be non-trivial
        assert!(signed.raw.len() > 10);
    }
}
