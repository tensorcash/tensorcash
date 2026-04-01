// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Type definitions for bulletin board trading system

use crate::bulletin_board::governance::OwnershipProof;
use serde::{Deserialize, Serialize};

/// Type of offer (buy, sell, swap, or contract)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum OfferType {
    Buy,
    Sell,
    Swap,
    /// Repo contract offer
    RepoContract,
    /// Forward/DvP contract offer
    ForwardContract,
    /// Spot contract offer
    SpotContract,
    /// Difficulty-derivative contract offer (CFD/option on mining difficulty)
    DifficultyContract,
}

/// Contract type for contract offers
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum ContractType {
    Repo,
    Forward,
    Spot,
    Difficulty,
}

/// State of an offer in its lifecycle
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum OfferState {
    /// Offer is visible on Nostr, awaiting taker
    Posted,
    /// Taker has requested trade (stores taker_pubkey)
    Requested(String),
    /// Maker has accepted request (stores taker_pubkey)
    Accepted(String),
    /// Both parties are in handshake_auto
    Handshaking,
    /// Trade is in progress
    Active,
    /// Trade completed successfully
    Completed,
    /// Maker cancelled the offer
    Cancelled,
    /// Offer expired (TTL reached)
    Expired,
}

/// Trade offer published to bulletin board
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Offer {
    /// Unique offer identifier (UUID)
    pub id: String,

    /// Unix timestamp when offer was created
    pub created_at: u64,

    /// Unix timestamp when offer expires (default 24h)
    pub expires_at: u64,

    /// Current state of the offer
    pub state: OfferState,

    /// Bitcoin network this offer is for (main, signet, testnet3, regtest)
    /// Used to compartmentalize offers by chain
    pub network: String,

    // Trade terms
    /// Type of offer (buy/sell/swap)
    pub offer_type: OfferType,

    /// Asset being sent (e.g., "BTC", "USD")
    pub asset_send: String,

    /// Asset being received (e.g., "USD", "BTC")
    pub asset_recv: String,

    /// Amount of asset_send
    pub amount: f64,

    /// Exchange rate
    pub price: f64,

    // Constraints
    /// Accepted payment methods (e.g., ["bank_transfer", "cash"])
    pub payment_methods: Vec<String>,

    /// Allowed regions (e.g., ["EU", "US"])
    pub regions: Vec<String>,

    // Security
    /// Maker's Nostr public key (npub1...)
    pub maker_pubkey: String,

    /// Whether escrow is required
    pub requires_escrow: bool,

    /// Minimum reputation score for taker
    pub min_reputation_score: f32,

    // Connection (populated after accept_trade_request)
    /// Session ID (only set after trade accepted)
    pub session_id: Option<String>,

    /// Ephemeral invite link (single-use, 10min TTL)
    /// SECURITY: Never serialized in public list_offers responses
    #[serde(skip_serializing)]
    pub invite_link: Option<String>,

    // Nostr metadata
    /// Nostr event ID (for deletion/updates)
    pub nostr_event_id: Option<String>,

    // Contract-specific fields (only populated for contract offers)
    /// Contract type (repo, forward, spot) - only for contract offers
    pub contract_type: Option<ContractType>,

    /// Full contract payload (Base64-encoded contract JSON from repo.propose/forward.propose)
    pub contract_payload: Option<String>,

    /// Maker's role in the contract ("lender", "borrower", "long", "short")
    pub maker_role: Option<String>,

    /// Pre-computed: Annual percentage rate (for display/search)
    pub apr: Option<f64>,

    /// Pre-computed: Loan-to-value ratio (repo contracts only)
    pub ltv: Option<f64>,

    /// Pre-computed: Tenor in days until maturity
    pub tenor_days: Option<u32>,

    /// Optional proof of funds (BIP-322 ownership proofs for maker's assets)
    /// Array allows proving multiple UTXOs or multiple assets in contract
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proof_of_funds: Option<Vec<OwnershipProof>>,
}

impl Offer {
    /// Create a new offer with default values
    ///
    /// # Arguments
    ///
    /// * `offer_type` - Type of offer (buy/sell/swap)
    /// * `asset_send` - Asset being sent
    /// * `asset_recv` - Asset being received
    /// * `amount` - Amount of asset_send
    /// * `price` - Exchange rate
    /// * `maker_pubkey` - Maker's Nostr public key
    /// * `network` - Bitcoin network (main, signet, testnet3, regtest)
    pub fn new(
        offer_type: OfferType,
        asset_send: String,
        asset_recv: String,
        amount: f64,
        price: f64,
        maker_pubkey: String,
        network: String,
    ) -> Self {
        let now = chrono::Utc::now().timestamp() as u64;
        let expires_at = now + 86400; // 24h default TTL

        Self {
            id: uuid::Uuid::new_v4().to_string(),
            created_at: now,
            expires_at,
            state: OfferState::Posted,
            network,
            offer_type,
            asset_send,
            asset_recv,
            amount,
            price,
            payment_methods: Vec::new(),
            regions: Vec::new(),
            maker_pubkey,
            requires_escrow: false,
            min_reputation_score: 0.0,
            session_id: None,
            invite_link: None,
            nostr_event_id: None,
            contract_type: None,
            contract_payload: None,
            maker_role: None,
            apr: None,
            ltv: None,
            tenor_days: None,
            proof_of_funds: None,
        }
    }

    /// Create a new contract offer
    ///
    /// # Arguments
    ///
    /// * `contract_type` - Type of contract (repo/forward/spot)
    /// * `contract_payload` - Full contract JSON payload
    /// * `maker_role` - Maker's role in the contract
    /// * `maker_pubkey` - Maker's Nostr public key
    /// * `network` - Bitcoin network (main, signet, testnet3, regtest)
    /// * `apr` - Annual percentage rate
    /// * `ltv` - Loan-to-value ratio
    /// * `tenor_days` - Days until maturity
    #[allow(clippy::too_many_arguments)]
    pub fn new_contract(
        contract_type: ContractType,
        contract_payload: String,
        maker_role: String,
        maker_pubkey: String,
        network: String,
        apr: Option<f64>,
        ltv: Option<f64>,
        tenor_days: Option<u32>,
    ) -> Self {
        let now = chrono::Utc::now().timestamp() as u64;
        let expires_at = now + 86400; // 24h default TTL

        // Determine offer_type from contract_type
        let offer_type = match contract_type {
            ContractType::Repo => OfferType::RepoContract,
            ContractType::Forward => OfferType::ForwardContract,
            ContractType::Spot => OfferType::SpotContract,
            ContractType::Difficulty => OfferType::DifficultyContract,
        };

        Self {
            id: uuid::Uuid::new_v4().to_string(),
            created_at: now,
            expires_at,
            state: OfferState::Posted,
            network,
            offer_type,
            asset_send: String::new(), // Not used for contracts
            asset_recv: String::new(), // Not used for contracts
            amount: 0.0,               // Not used for contracts
            price: 0.0,                // Not used for contracts
            payment_methods: Vec::new(),
            regions: Vec::new(),
            maker_pubkey,
            requires_escrow: true, // Contracts always use escrow
            min_reputation_score: 0.0,
            session_id: None,
            invite_link: None,
            nostr_event_id: None,
            contract_type: Some(contract_type),
            contract_payload: Some(contract_payload),
            maker_role: Some(maker_role),
            apr,
            ltv,
            tenor_days,
            proof_of_funds: None,
        }
    }

    /// Check if this is a contract offer
    #[allow(dead_code)]
    pub fn is_contract(&self) -> bool {
        self.contract_type.is_some()
    }

    /// Check if offer has expired
    pub fn is_expired(&self) -> bool {
        let now = chrono::Utc::now().timestamp() as u64;
        now > self.expires_at
    }

    /// Check if offer can accept new trade requests
    pub fn can_accept_requests(&self) -> bool {
        matches!(self.state, OfferState::Posted) && !self.is_expired()
    }
}

/// Status of a trade request
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum RequestStatus {
    Pending,
    Accepted,
    Rejected,
    Cancelled,
}

/// Trade request from taker to maker
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeRequest {
    /// Unique request identifier
    pub id: String,

    /// Offer ID this request is for
    pub offer_id: String,

    /// Taker's Nostr public key
    pub taker_pubkey: String,

    /// Maker's Nostr public key
    pub maker_pubkey: String,

    /// Unix timestamp when request was created
    pub timestamp: u64,

    /// Optional message from taker
    pub message: Option<String>,

    /// Status of the request
    pub status: RequestStatus,

    /// Optional invite link sent after acceptance
    pub invite_link: Option<String>,

    /// Optional invite expiry timestamp (epoch seconds)
    pub invite_expires_at: Option<u64>,

    /// Unix timestamp when this request was last updated
    pub updated_at: u64,

    /// Optional proof of funds (BIP-322 ownership proofs for taker's assets)
    /// Array allows proving multiple UTXOs or multiple assets in contract
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proof_of_funds: Option<Vec<OwnershipProof>>,
}

impl TradeRequest {
    /// Create a new trade request
    pub fn new(
        offer_id: String,
        taker_pubkey: String,
        maker_pubkey: String,
        message: Option<String>,
    ) -> Self {
        let now = chrono::Utc::now().timestamp() as u64;
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            offer_id,
            taker_pubkey,
            maker_pubkey,
            timestamp: now,
            message,
            status: RequestStatus::Pending,
            invite_link: None,
            invite_expires_at: None,
            updated_at: now,
            proof_of_funds: None,
        }
    }
}

/// Direction of a trade request from the viewer's perspective
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum RequestDirection {
    Incoming,
    Outgoing,
}

/// Minimal snapshot of an offer for request listings
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OfferSummary {
    pub id: String,
    pub network: String,
    pub offer_type: OfferType,
    pub asset_send: String,
    pub asset_recv: String,
    pub amount: f64,
    pub price: f64,
    pub maker_pubkey: String,
    pub expires_at: u64,
    pub state: OfferState,
    pub requires_escrow: bool,
    pub payment_methods: Vec<String>,
    pub regions: Vec<String>,
    // Contract-specific fields
    pub contract_type: Option<ContractType>,
    pub maker_role: Option<String>,
    pub apr: Option<f64>,
    pub ltv: Option<f64>,
    pub tenor_days: Option<u32>,
    pub proof_of_funds: Option<Vec<OwnershipProof>>,
}

impl From<Offer> for OfferSummary {
    fn from(offer: Offer) -> Self {
        Self {
            id: offer.id,
            network: offer.network,
            offer_type: offer.offer_type,
            asset_send: offer.asset_send,
            asset_recv: offer.asset_recv,
            amount: offer.amount,
            price: offer.price,
            maker_pubkey: offer.maker_pubkey,
            expires_at: offer.expires_at,
            state: offer.state,
            requires_escrow: offer.requires_escrow,
            payment_methods: offer.payment_methods,
            regions: offer.regions,
            contract_type: offer.contract_type,
            maker_role: offer.maker_role,
            apr: offer.apr,
            ltv: offer.ltv,
            tenor_days: offer.tenor_days,
            proof_of_funds: offer.proof_of_funds,
        }
    }
}

impl From<&Offer> for OfferSummary {
    fn from(offer: &Offer) -> Self {
        Self {
            id: offer.id.clone(),
            network: offer.network.clone(),
            offer_type: offer.offer_type.clone(),
            asset_send: offer.asset_send.clone(),
            asset_recv: offer.asset_recv.clone(),
            amount: offer.amount,
            price: offer.price,
            maker_pubkey: offer.maker_pubkey.clone(),
            expires_at: offer.expires_at,
            state: offer.state.clone(),
            requires_escrow: offer.requires_escrow,
            payment_methods: offer.payment_methods.clone(),
            regions: offer.regions.clone(),
            contract_type: offer.contract_type.clone(),
            maker_role: offer.maker_role.clone(),
            apr: offer.apr,
            ltv: offer.ltv,
            tenor_days: offer.tenor_days,
            proof_of_funds: offer.proof_of_funds.clone(),
        }
    }
}

/// Trade request decorated with viewer-specific metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeRequestSummary {
    #[serde(flatten)]
    pub request: TradeRequest,
    pub direction: RequestDirection,
    pub counterparty_pubkey: String,
    pub offer: Option<OfferSummary>,
}

/// Filters for querying offers
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OfferFilters {
    /// Filter by Bitcoin network (main, signet, testnet3, regtest)
    /// This is automatically set by the manager based on init_bb network parameter
    pub network: Option<String>,

    /// Filter by offer type
    pub offer_type: Option<String>,

    /// Minimum amount
    pub min_amount: Option<f64>,

    /// Maximum amount
    pub max_amount: Option<f64>,

    /// Filter by region
    pub region: Option<String>,

    /// Filter by payment method
    pub payment_method: Option<String>,

    /// Filter by minimum reputation score
    pub min_reputation: Option<f32>,

    /// Filter by contract type ("repo", "forward", "spot")
    pub contract_type: Option<String>,

    /// Filter by maker role ("lender", "borrower", "long", "short")
    pub maker_role: Option<String>,

    /// Filter by minimum APR
    pub min_apr: Option<f64>,

    /// Filter by maximum APR
    pub max_apr: Option<f64>,

    /// Filter by minimum tenor (days)
    pub min_tenor_days: Option<u32>,

    /// Filter by maximum tenor (days)
    pub max_tenor_days: Option<u32>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_offer_creation() {
        let offer = Offer::new(
            OfferType::Sell,
            "BTC".to_string(),
            "USD".to_string(),
            0.1,
            65000.0,
            "npub1test".to_string(),
            "regtest".to_string(),
        );

        assert_eq!(offer.offer_type, OfferType::Sell);
        assert_eq!(offer.amount, 0.1);
        assert_eq!(offer.price, 65000.0);
        assert_eq!(offer.state, OfferState::Posted);
        assert_eq!(offer.network, "regtest");
        assert!(!offer.is_expired());
    }

    #[test]
    fn test_offer_expiry() {
        let mut offer = Offer::new(
            OfferType::Buy,
            "USD".to_string(),
            "BTC".to_string(),
            1000.0,
            0.000015,
            "npub1test".to_string(),
            "main".to_string(),
        );

        // Set expiry to past
        offer.expires_at = 0;
        assert!(offer.is_expired());
        assert!(!offer.can_accept_requests());
    }

    #[test]
    fn test_trade_request_creation() {
        let request = TradeRequest::new(
            "offer123".to_string(),
            "npub1taker".to_string(),
            "npub1maker".to_string(),
            Some("I'm interested".to_string()),
        );

        assert_eq!(request.status, RequestStatus::Pending);
        assert_eq!(request.offer_id, "offer123");
        assert!(request.message.is_some());
        assert!(request.invite_link.is_none());
        assert!(request.invite_expires_at.is_none());
        assert!(request.updated_at >= request.timestamp);
    }

    #[test]
    fn test_offer_state_transitions() {
        let state = OfferState::Posted;
        assert_eq!(state, OfferState::Posted);

        let state = OfferState::Requested("npub1taker".to_string());
        if let OfferState::Requested(pubkey) = state {
            assert_eq!(pubkey, "npub1taker");
        } else {
            panic!("Expected Requested state");
        }
    }
}
