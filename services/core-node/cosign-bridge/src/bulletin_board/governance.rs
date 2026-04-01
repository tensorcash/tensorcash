// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Governance proposal types and utilities
//!
//! This module defines the data structures for asset governance rotation proposals
//! that are broadcast via Nostr bulletin board.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Governance proposal flow type
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum FlowType {
    /// Public flow: ICU text visible to all
    Public,
    /// Private flow: ICU text only shared with verified holders
    Private,
}

/// Governance DM message types
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum GovernanceDMType {
    /// Holder requests access to private proposal
    AccessRequest,
    /// Issuer responds with proposal details
    ProposalResponse,
    /// Holder submits ballot
    Ballot,
    /// Issuer acknowledges ballot receipt
    BallotReceipt,
}

/// Envelope for all governance DMs (replay protection + type safety)
///
/// Replaces blind struct parsing with explicit message typing and sequencing.
/// Provides replay protection via sequence numbers and previous message hashing.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceDMEnvelope {
    /// Protocol version (currently 1)
    pub version: u32,

    /// Message type discriminator
    pub message_type: GovernanceDMType,

    /// Proposal ID this message relates to
    pub proposal_id: String,

    /// Sequence number (per proposal conversation, starts at 1)
    /// - AccessRequest: 1
    /// - ProposalResponse: 2
    /// - Ballot: 3
    /// - BallotReceipt: 4
    pub sequence: u64,

    /// SHA256 hash of previous envelope in this conversation (for chain integrity)
    /// None for first message (AccessRequest)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prev_hash: Option<String>,

    /// Message creation timestamp (seconds since epoch)
    pub timestamp: u64,

    /// Message expiry (seconds since epoch)
    /// Messages received after this time are rejected
    pub expiry: u64,

    /// JSON-serialized inner message (GovernanceAccessRequest, etc.)
    pub payload: String,
}

impl GovernanceDMEnvelope {
    /// Compute SHA256 hash of this envelope for chaining
    pub fn compute_hash(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.version.to_string().as_bytes());
        hasher.update(
            serde_json::to_string(&self.message_type)
                .unwrap_or_default()
                .as_bytes(),
        );
        hasher.update(self.proposal_id.as_bytes());
        hasher.update(self.sequence.to_string().as_bytes());
        if let Some(ref prev) = self.prev_hash {
            hasher.update(prev.as_bytes());
        }
        hasher.update(self.timestamp.to_string().as_bytes());
        hasher.update(self.expiry.to_string().as_bytes());
        hasher.update(self.payload.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    /// Validate envelope structure and timing
    pub fn validate(&self, now: u64, expected_prev_hash: Option<&str>) -> Result<(), String> {
        // Check version
        if self.version != 1 {
            return Err(format!("Unsupported envelope version: {}", self.version));
        }

        // Check expiry
        if now > self.expiry {
            return Err(format!(
                "Envelope expired at {}, now is {}",
                self.expiry, now
            ));
        }

        // Check timestamp is not in the future (allow 5min clock skew)
        if self.timestamp > now + 300 {
            return Err(format!(
                "Envelope timestamp {} is in the future (now: {})",
                self.timestamp, now
            ));
        }

        // Check sequence numbering matches message type
        let expected_seq = match self.message_type {
            GovernanceDMType::AccessRequest => 1,
            GovernanceDMType::ProposalResponse => 2,
            GovernanceDMType::Ballot => 3,
            GovernanceDMType::BallotReceipt => 4,
        };
        if self.sequence != expected_seq {
            return Err(format!(
                "Sequence mismatch: expected {}, got {}",
                expected_seq, self.sequence
            ));
        }

        // Check prev_hash chain integrity
        if self.sequence == 1 {
            if self.prev_hash.is_some() {
                return Err("First message should not have prev_hash".to_string());
            }
        } else {
            match (&self.prev_hash, expected_prev_hash) {
                (None, _) => return Err(format!("Message {} requires prev_hash", self.sequence)),
                (Some(actual), Some(expected)) if actual != expected => {
                    return Err(format!(
                        "prev_hash mismatch: expected {}, got {}",
                        expected, actual
                    ));
                }
                (Some(_), None) => return Err("prev_hash provided but not expected".to_string()),
                (Some(_), Some(_)) => {} // Match OK
            }
        }

        Ok(())
    }
}

/// BIP-322 attestation proving control of an address
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IcuAttestation {
    /// Bitcoin address controlling the ICU UTXO
    pub address: String,

    /// Signed message: "TENSORCASH_GOVERNANCE:{proposal_id}"
    pub message: String,

    /// Base64-encoded signature
    pub signature: String,
}

/// Current asset policy parameters (from getassetpolicy RPC)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PolicyParams {
    /// Quorum threshold in basis points (e.g., 5500 = 55%)
    pub policy_quorum_bps: u32,

    /// Issuance cap in asset units
    pub issuance_cap_units: u64,

    /// Policy epoch number
    pub policy_epoch: u64,

    /// Total units issued (optional)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub issued_total: Option<u64>,

    /// Total units burned (optional)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub burned_total: Option<u64>,
}

/// Proposed policy changes (delta only - omit unchanged fields)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PolicyDelta {
    /// Proposed quorum threshold
    #[serde(skip_serializing_if = "Option::is_none")]
    pub policy_quorum_bps: Option<u32>,

    /// Proposed issuance cap
    #[serde(skip_serializing_if = "Option::is_none")]
    pub issuance_cap_units: Option<u64>,
}

/// Optional governance proposal metadata
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct GovernanceMetadata {
    /// Short proposal title
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,

    /// Brief proposal description
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,

    /// URL for discussion (forum, GitHub issue, etc.)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub discussion_url: Option<String>,
}

/// Complete governance proposal structure
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceProposal {
    /// Schema version
    pub version: u32,

    /// Unique proposal identifier: SHA256(asset_id || created_at || nonce)
    pub proposal_id: String,

    /// Hexadecimal asset identifier
    pub asset_id: String,

    /// Issuer's Nostr public key (hex format)
    pub issuer_nostr_pubkey: String,

    /// Transaction ID of current ICU UTXO
    pub icu_txid: String,

    /// Output index of current ICU UTXO
    pub icu_vout: u32,

    /// BIP-322 proof of ICU control
    pub icu_attestation: IcuAttestation,

    /// Current policy parameters
    pub current_policy: PolicyParams,

    /// Proposed policy changes
    pub proposed_policy: PolicyDelta,

    /// SHA256 hash of canonical ICU text (little-endian hex)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub canonical_icu_hash: Option<String>,

    /// Human-readable governance document (required for public flow)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub icu_text: Option<String>,

    /// Witness bundle JSON (encrypted master key shares for holder_only decryption)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness_bundle: Option<String>,

    /// SHA256 hash of witness bundle (little-endian hex)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness_bundle_hash: Option<String>,

    /// Compression flag (0=none, 1=zstd) - needed for ICU payload reconstruction
    #[serde(skip_serializing_if = "Option::is_none")]
    pub icu_compression: Option<u8>,

    /// SHA256 hash of the template PSBT from prepare_rotation RPC
    pub template_psbt_hash: String,

    /// Template PSBT from prepare_rotation (for public flow only)
    /// Base64-encoded PSBT that holders will sign to vote
    /// For private flow, this is sent via DM instead of broadcast
    #[serde(skip_serializing_if = "Option::is_none")]
    pub template_psbt: Option<String>,

    /// Unix timestamp when proposal was created
    pub created_at: u64,

    /// Unix timestamp when proposal expires
    pub expires_at: u64,

    /// Flow type (public or private)
    pub flow_type: FlowType,

    /// Optional additional metadata
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<GovernanceMetadata>,

    /// Nostr event ID (populated after publishing)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nostr_event_id: Option<String>,
}

impl GovernanceProposal {
    /// Create a new governance proposal
    #[allow(clippy::too_many_arguments)]
    #[allow(dead_code)] // Used by external callers
    pub fn new(
        asset_id: String,
        issuer_nostr_pubkey: String,
        icu_txid: String,
        icu_vout: u32,
        icu_attestation: IcuAttestation,
        current_policy: PolicyParams,
        proposed_policy: PolicyDelta,
        template_psbt_hash: String,
        expires_at: u64,
        flow_type: FlowType,
    ) -> Self {
        let created_at = chrono::Utc::now().timestamp() as u64;
        let nonce = rand::random::<u64>();

        // Generate proposal_id: SHA256(asset_id || created_at || nonce)
        let proposal_data = format!("{}{}{}", asset_id, created_at, nonce);
        let mut hasher = Sha256::new();
        hasher.update(proposal_data.as_bytes());
        let proposal_id = format!("{:x}", hasher.finalize());

        Self {
            version: 1,
            proposal_id,
            asset_id,
            issuer_nostr_pubkey,
            icu_txid,
            icu_vout,
            icu_attestation,
            current_policy,
            proposed_policy,
            canonical_icu_hash: None,
            icu_text: None,
            witness_bundle: None,
            witness_bundle_hash: None,
            icu_compression: None,
            template_psbt_hash,
            template_psbt: None,
            created_at,
            expires_at,
            flow_type,
            metadata: None,
            nostr_event_id: None,
        }
    }

    /// Check if proposal has expired
    pub fn is_expired(&self) -> bool {
        let now = chrono::Utc::now().timestamp() as u64;
        now >= self.expires_at
    }

    /// Validate proposal structure
    pub fn validate(&self) -> Result<(), String> {
        // Version check
        if self.version != 1 {
            return Err(format!("Unsupported version: {}", self.version));
        }

        // Timestamp validation
        if self.expires_at <= self.created_at {
            return Err("expires_at must be greater than created_at".to_string());
        }

        // Hash format validation (64-char hex)
        let hex_pattern = regex::Regex::new(r"^[a-f0-9]{64}$").unwrap();

        if !hex_pattern.is_match(&self.proposal_id) {
            return Err("Invalid proposal_id format".to_string());
        }

        if !hex_pattern.is_match(&self.asset_id) {
            return Err("Invalid asset_id format".to_string());
        }

        if !hex_pattern.is_match(&self.issuer_nostr_pubkey) {
            return Err("Invalid issuer_nostr_pubkey format".to_string());
        }

        if !hex_pattern.is_match(&self.icu_txid) {
            return Err("Invalid icu_txid format".to_string());
        }

        if !hex_pattern.is_match(&self.template_psbt_hash) {
            return Err("Invalid template_psbt_hash format".to_string());
        }

        // Validate optional hash fields when present
        if let Some(ref canonical_hash) = self.canonical_icu_hash {
            if !hex_pattern.is_match(canonical_hash) {
                return Err("Invalid canonical_icu_hash format (must be 64-char hex)".to_string());
            }
        }

        if let Some(ref witness_hash) = self.witness_bundle_hash {
            if !hex_pattern.is_match(witness_hash) {
                return Err("Invalid witness_bundle_hash format (must be 64-char hex)".to_string());
            }
        }

        // Flow-specific validation
        match self.flow_type {
            FlowType::Public => {
                if self.icu_text.is_none() {
                    return Err("icu_text is required for public flow".to_string());
                }
                if self.template_psbt.is_none() {
                    return Err("template_psbt is required for public flow".to_string());
                }
            }
            FlowType::Private => {
                if self.icu_text.is_some() {
                    return Err("icu_text must not be present for private flow".to_string());
                }
                if self.template_psbt.is_some() {
                    return Err(
                        "template_psbt must not be present for private flow (sent via DM)"
                            .to_string(),
                    );
                }
            }
        }

        // Policy delta must have at least one change
        if self.proposed_policy.policy_quorum_bps.is_none()
            && self.proposed_policy.issuance_cap_units.is_none()
        {
            return Err("proposed_policy must have at least one change".to_string());
        }

        Ok(())
    }

    /// Verify ICU attestation (requires RPC call to verifymessage)
    #[allow(dead_code)] // Used by Qt validation
    pub fn verify_icu_attestation_message(&self) -> bool {
        let expected_message = format!("TENSORCASH_GOVERNANCE:{}", self.proposal_id);
        self.icu_attestation.message == expected_message
    }
}

/// Lightweight summary of a governance proposal for listing
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceProposalSummary {
    pub proposal_id: String,
    pub asset_id: String,
    pub issuer_nostr_pubkey: String,
    pub created_at: u64,
    pub expires_at: u64,
    pub flow_type: FlowType,
    pub title: Option<String>,
    pub is_expired: bool,
    pub policy_changes: String, // Human-readable summary
}

impl From<&GovernanceProposal> for GovernanceProposalSummary {
    fn from(proposal: &GovernanceProposal) -> Self {
        let mut changes = Vec::new();

        if let Some(quorum) = proposal.proposed_policy.policy_quorum_bps {
            changes.push(format!("Quorum: {}%", quorum as f64 / 100.0));
        }

        if let Some(cap) = proposal.proposed_policy.issuance_cap_units {
            changes.push(format!("Cap: {}", cap));
        }

        let policy_changes = changes.join(", ");

        Self {
            proposal_id: proposal.proposal_id.clone(),
            asset_id: proposal.asset_id.clone(),
            issuer_nostr_pubkey: proposal.issuer_nostr_pubkey.clone(),
            created_at: proposal.created_at,
            expires_at: proposal.expires_at,
            flow_type: proposal.flow_type.clone(),
            title: proposal.metadata.as_ref().and_then(|m| m.title.clone()),
            is_expired: proposal.is_expired(),
            policy_changes,
        }
    }
}

/// Governance ballot - holder's signed vote on a proposal
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceBallot {
    /// Schema version (currently 1)
    pub version: u32,

    /// Proposal ID this ballot is voting on
    pub proposal_id: String,

    /// Asset ID
    pub asset_id: String,

    /// Voter's Nostr public key (optional for anonymous ballots)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub voter_nostr_pubkey: Option<String>,

    /// Signed PSBT containing holder's voting inputs
    pub signed_psbt: String,

    /// Total voting units contributed by this ballot
    pub ballot_units: u64,

    /// Unix timestamp when ballot was signed
    pub voter_timestamp: u64,

    /// Nostr event ID where this ballot was published (set by bulletin board)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nostr_event_id: Option<String>,
}

impl GovernanceBallot {
    /// Create new ballot
    #[allow(dead_code)]
    pub fn new(
        proposal_id: String,
        asset_id: String,
        signed_psbt: String,
        ballot_units: u64,
    ) -> Self {
        Self {
            version: 1,
            proposal_id,
            asset_id,
            voter_nostr_pubkey: None,
            signed_psbt,
            ballot_units,
            voter_timestamp: chrono::Utc::now().timestamp() as u64,
            nostr_event_id: None,
        }
    }

    /// Validate ballot structure
    pub fn validate(&self) -> Result<(), String> {
        // Version check
        if self.version != 1 {
            return Err(format!("Unsupported ballot version: {}", self.version));
        }

        // Hash format validation (64-char hex)
        let hex_pattern = regex::Regex::new(r"^[a-f0-9]{64}$").unwrap();

        if !hex_pattern.is_match(&self.proposal_id) {
            return Err("Invalid proposal_id format".to_string());
        }

        if !hex_pattern.is_match(&self.asset_id) {
            return Err("Invalid asset_id format".to_string());
        }

        if self.signed_psbt.is_empty() {
            return Err("Empty signed_psbt".to_string());
        }

        if self.ballot_units == 0 {
            return Err("ballot_units must be greater than 0".to_string());
        }

        Ok(())
    }
}

/// ==== Private Governance DM Protocol ====
///
/// These messages are exchanged over encrypted Nostr DMs (NIP-04/NIP-44)
/// for private governance flows where ICU text and template PSBTs are not
/// broadcast publicly.
///
/// Holder's request to access a private governance proposal
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceAccessRequest {
    /// Protocol version
    pub version: u32,

    /// Proposal ID being requested
    pub proposal_id: String,

    /// Asset ID
    pub asset_id: String,

    /// Holder's Nostr public key
    pub holder_nostr_pubkey: String,

    /// BIP-322 ownership proof over holder's asset UTXO
    /// Message: "TENSORCASH_HOLDER:{proposal_id}:{holder_nostr_pubkey}"
    pub ownership_proof: OwnershipProof,

    /// Request timestamp
    pub requested_at: u64,

    /// Optional: Sequence number for replay protection
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sequence: Option<u64>,
}

/// BIP-322 ownership proof structure
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnershipProof {
    /// UTXO being proven (txid:vout)
    pub utxo_ref: String,

    /// Bitcoin address controlling the UTXO
    pub address: String,

    /// Expected message format: "TENSORCASH_HOLDER:{proposal_id}:{holder_nostr_pubkey}"
    /// or "TENSORCASH_PROOF:{offer_id}:{role}:{asset_id}" for contract offers
    pub message: String,

    /// BIP-322 signature
    pub signature: String,

    /// Asset units held in this UTXO
    pub asset_units: u64,

    /// Asset ID (hex string) - optional for backward compatibility with governance
    /// Required for contract offer proofs to support multi-asset contracts
    #[serde(skip_serializing_if = "Option::is_none")]
    pub asset_id: Option<String>,
}

/// Issuer's response with full proposal details
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceProposalResponse {
    /// Protocol version
    pub version: u32,

    /// Proposal ID
    pub proposal_id: String,

    /// Full ICU governance text (was not in public broadcast)
    pub icu_text: String,

    /// Canonical ICU hash (for holder to verify)
    pub canonical_icu_hash: String,

    /// Witness bundle JSON (if any)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness_bundle: Option<String>,

    /// Witness bundle hash (for holder to verify)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness_bundle_hash: Option<String>,

    /// Template PSBT for ballot signing
    pub template_psbt: String,

    /// Template PSBT hash (for holder to verify)
    pub template_psbt_hash: String,

    /// Response timestamp
    pub responded_at: u64,

    /// Sequence number (must match request + 1 for replay protection)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sequence: Option<u64>,
}

/// Holder's signed ballot submission (via DM for private flow)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceBallotDM {
    /// Protocol version
    pub version: u32,

    /// Proposal ID
    pub proposal_id: String,

    /// Asset ID
    pub asset_id: String,

    /// Holder's signed PSBT (ballot)
    pub signed_psbt: String,

    /// Voting units (sum of selected UTXOs)
    pub ballot_units: u64,

    /// Ballot timestamp
    pub ballot_timestamp: u64,

    /// Sequence number for replay protection
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sequence: Option<u64>,
}

/// Issuer's receipt confirmation
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GovernanceBallotReceipt {
    /// Protocol version
    pub version: u32,

    /// Proposal ID
    pub proposal_id: String,

    /// Ballot ID (SHA256 of signed_psbt)
    pub ballot_id: String,

    /// Units accepted
    pub units_accepted: u64,

    /// Receipt timestamp
    pub receipt_timestamp: u64,

    /// Optional: Current quorum status
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quorum_status: Option<QuorumStatus>,
}

/// Current quorum aggregation status
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuorumStatus {
    /// Total units voted so far
    pub total_voted_units: u64,

    /// Total settled supply for this asset
    pub settled_supply: u64,

    /// Quorum threshold in basis points
    pub quorum_bps: u32,

    /// Whether quorum has been reached
    pub quorum_reached: bool,
}

impl GovernanceAccessRequest {
    /// Create a new access request
    pub fn new(
        proposal_id: String,
        asset_id: String,
        holder_nostr_pubkey: String,
        ownership_proof: OwnershipProof,
    ) -> Self {
        Self {
            version: 1,
            proposal_id,
            asset_id,
            holder_nostr_pubkey,
            ownership_proof,
            requested_at: chrono::Utc::now().timestamp() as u64,
            sequence: None,
        }
    }

    /// Validate request structure and ownership proof
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 {
            return Err(format!("Unsupported version: {}", self.version));
        }

        let hex_pattern = regex::Regex::new(r"^[a-f0-9]{64}$").unwrap();
        if !hex_pattern.is_match(&self.proposal_id) {
            return Err("Invalid proposal_id format".to_string());
        }
        if !hex_pattern.is_match(&self.asset_id) {
            return Err("Invalid asset_id format".to_string());
        }
        if !hex_pattern.is_match(&self.holder_nostr_pubkey) {
            return Err("Invalid holder_nostr_pubkey format".to_string());
        }

        // Validate ownership proof message format
        let expected_message = format!(
            "TENSORCASH_HOLDER:{}:{}",
            self.proposal_id, self.holder_nostr_pubkey
        );
        if self.ownership_proof.message != expected_message {
            return Err(format!(
                "Ownership proof message mismatch: expected '{}', got '{}'",
                expected_message, self.ownership_proof.message
            ));
        }

        if self.ownership_proof.asset_units == 0 {
            return Err("asset_units must be greater than 0".to_string());
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_test_proposal() -> GovernanceProposal {
        let icu_attestation = IcuAttestation {
            address: "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh".to_string(),
            message: "TENSORCASH_GOVERNANCE:test".to_string(),
            signature: "H1234567890abcdef".to_string(),
        };

        let current_policy = PolicyParams {
            policy_quorum_bps: 5500,
            issuance_cap_units: 100,
            policy_epoch: 1,
            issued_total: Some(100),
            burned_total: Some(0),
        };

        let mut proposed_policy = PolicyDelta {
            policy_quorum_bps: None,
            issuance_cap_units: None,
        };
        proposed_policy.issuance_cap_units = Some(200);

        GovernanceProposal::new(
            "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef".to_string(),
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890".to_string(),
            "fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321".to_string(),
            0,
            icu_attestation,
            current_policy,
            proposed_policy,
            "9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba".to_string(),
            chrono::Utc::now().timestamp() as u64 + 86400, // expires in 24h
            FlowType::Public,
        )
    }

    #[test]
    fn test_proposal_creation() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test governance document".to_string());

        assert_eq!(proposal.version, 1);
        assert_eq!(proposal.flow_type, FlowType::Public);
        assert!(!proposal.is_expired());
        assert!(proposal.proposal_id.len() == 64); // SHA256 hex
    }

    #[test]
    fn test_proposal_validation_public() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test governance document".to_string());
        proposal.template_psbt = Some("cHNidP8BAH...test_psbt".to_string()); // Mock PSBT

        // Update attestation message to match proposal_id
        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        assert!(proposal.validate().is_ok());
    }

    #[test]
    fn test_proposal_validation_private() {
        let mut proposal = create_test_proposal();
        proposal.flow_type = FlowType::Private;
        proposal.icu_text = None; // Private flow must not have icu_text

        // Update attestation message
        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        assert!(proposal.validate().is_ok());
    }

    #[test]
    fn test_proposal_validation_fails_no_icu_text_public() {
        let mut proposal = create_test_proposal();
        proposal.flow_type = FlowType::Public;
        proposal.icu_text = None;

        // Update attestation message
        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        let result = proposal.validate();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("icu_text is required"));
    }

    #[test]
    fn test_proposal_validation_fails_private_with_icu_text() {
        let mut proposal = create_test_proposal();
        proposal.flow_type = FlowType::Private;
        proposal.icu_text = Some("Should not be present".to_string());

        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        let result = proposal.validate();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("must not be present"));
    }

    #[test]
    fn test_proposal_validation_fails_no_policy_changes() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test".to_string());
        proposal.template_psbt = Some("cHNidP8BAH...test_psbt".to_string());
        proposal.proposed_policy = PolicyDelta {
            policy_quorum_bps: None,
            issuance_cap_units: None,
        };

        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        let result = proposal.validate();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("at least one change"));
    }

    #[test]
    fn test_proposal_validation_fails_invalid_expiry() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test".to_string());
        proposal.expires_at = proposal.created_at - 1; // Expires before creation

        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        let result = proposal.validate();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("expires_at must be greater"));
    }

    #[test]
    fn test_proposal_expiry() {
        let mut proposal = create_test_proposal();
        proposal.expires_at = chrono::Utc::now().timestamp() as u64 - 1;

        assert!(proposal.is_expired());
    }

    #[test]
    fn test_proposal_not_expired() {
        let proposal = create_test_proposal();
        assert!(!proposal.is_expired());
    }

    #[test]
    fn test_attestation_message_verification_valid() {
        let mut proposal = create_test_proposal();
        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        assert!(proposal.verify_icu_attestation_message());
    }

    #[test]
    fn test_attestation_message_verification_invalid() {
        let mut proposal = create_test_proposal();
        proposal.icu_attestation.message = "WRONG_FORMAT".to_string();

        assert!(!proposal.verify_icu_attestation_message());
    }

    #[test]
    fn test_proposal_summary_from_proposal() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test".to_string());
        proposal.metadata = Some(GovernanceMetadata {
            title: Some("Test Proposal".to_string()),
            description: Some("Description".to_string()),
            discussion_url: Some("https://example.com".to_string()),
        });

        let summary = GovernanceProposalSummary::from(&proposal);

        assert_eq!(summary.proposal_id, proposal.proposal_id);
        assert_eq!(summary.asset_id, proposal.asset_id);
        assert_eq!(summary.issuer_nostr_pubkey, proposal.issuer_nostr_pubkey);
        assert_eq!(summary.created_at, proposal.created_at);
        assert_eq!(summary.expires_at, proposal.expires_at);
        assert_eq!(summary.flow_type, proposal.flow_type);
        assert_eq!(summary.title, Some("Test Proposal".to_string()));
        assert!(!summary.is_expired);
        assert!(summary.policy_changes.contains("Cap: 200"));
    }

    #[test]
    fn test_proposal_summary_multiple_changes() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test".to_string());
        proposal.proposed_policy.policy_quorum_bps = Some(6000);

        let summary = GovernanceProposalSummary::from(&proposal);

        assert!(summary.policy_changes.contains("Quorum: 60%"));
        assert!(summary.policy_changes.contains("Cap: 200"));
    }

    #[test]
    fn test_json_serialization_roundtrip() {
        let mut proposal = create_test_proposal();
        proposal.icu_text = Some("Test governance document".to_string());
        proposal.icu_attestation.message =
            format!("TENSORCASH_GOVERNANCE:{}", proposal.proposal_id);

        // Serialize to JSON
        let json = serde_json::to_string(&proposal).unwrap();

        // Deserialize back
        let deserialized: GovernanceProposal = serde_json::from_str(&json).unwrap();

        assert_eq!(deserialized.proposal_id, proposal.proposal_id);
        assert_eq!(deserialized.asset_id, proposal.asset_id);
        assert_eq!(deserialized.icu_text, proposal.icu_text);
        assert_eq!(deserialized.flow_type, proposal.flow_type);
    }

    #[test]
    fn test_flow_type_serialization() {
        let public_json = serde_json::to_string(&FlowType::Public).unwrap();
        assert_eq!(public_json, "\"public\"");

        let private_json = serde_json::to_string(&FlowType::Private).unwrap();
        assert_eq!(private_json, "\"private\"");
    }

    #[test]
    fn test_flow_type_deserialization() {
        let public: FlowType = serde_json::from_str("\"public\"").unwrap();
        assert_eq!(public, FlowType::Public);

        let private: FlowType = serde_json::from_str("\"private\"").unwrap();
        assert_eq!(private, FlowType::Private);
    }

    #[test]
    fn test_policy_delta_at_least_one_field() {
        let delta = PolicyDelta {
            policy_quorum_bps: Some(6000),
            issuance_cap_units: None,
        };

        let json = serde_json::to_string(&delta).unwrap();
        assert!(json.contains("6000"));
        assert!(!json.contains("issuance_cap_units"));
    }

    #[test]
    fn test_icu_attestation_structure() {
        let attestation = IcuAttestation {
            address: "bc1qtest".to_string(),
            message: "TENSORCASH_GOVERNANCE:abc123".to_string(),
            signature: "sig123".to_string(),
        };

        let json = serde_json::to_string(&attestation).unwrap();
        assert!(json.contains("bc1qtest"));
        assert!(json.contains("TENSORCASH_GOVERNANCE"));
        assert!(json.contains("sig123"));
    }
}
