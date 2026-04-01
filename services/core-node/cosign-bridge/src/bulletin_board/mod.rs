// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Bulletin Board Trading System
//!
//! This module implements a decentralized bulletin board for discovering
//! and matchmaking P2P trades using Nostr as the discovery backend.
//!
//! ## Architecture
//!
//! - **Discovery Layer** (Public): Offers posted to Nostr relays (kind 30078)
//! - **Rendezvous Layer** (Private): Trade requests via Nostr DMs (kind 4)
//! - **Execution Layer** (Secure): Bilateral sessions via existing cosign-bridge
//!
//! ## Two-Phase Trading Flow
//!
//! 1. **Phase 1: Discovery**
//!    - Maker posts offer to Nostr (24h TTL)
//!    - Taker browses offers (from Nostr + local cache)
//!
//! 2. **Phase 2: Rendezvous**
//!    - Taker sends trade request via Nostr DM
//!    - Maker accepts → creates session → sends invite link via DM
//!    - Both parties join session and complete handshake
//!
//! ## Usage
//!
//! ```ignore
//! // Initialize bulletin board (called once at startup)
//! manager.init_bulletin_board(relays, key_path).await?;
//!
//! // Post an offer
//! let offer_id = bulletin_board.post_offer(offer).await?;
//!
//! // List offers
//! let offers = bulletin_board.list_offers(filters).await?;
//!
//! // Request trade (taker)
//! let request_id = bulletin_board.request_trade(offer_id, taker_pubkey).await?;
//!
//! // Accept trade request (maker)
//! bulletin_board.accept_trade_request(request_id, invite_link).await?;
//! ```

pub mod discussion;
pub mod governance;
pub mod manager;
pub mod nostr;
pub mod types;

// Public API - some types are only used in tests but need to be exported
#[allow(unused_imports)]
pub use discussion::{DiscussionPost, DiscussionScope, DISCUSSION_KIND, DISCUSSION_TOPIC};
#[allow(unused_imports)]
pub use governance::{
    FlowType, GovernanceMetadata, GovernanceProposal, GovernanceProposalSummary, IcuAttestation,
    PolicyDelta, PolicyParams,
};
pub use manager::BulletinBoardManager;
#[allow(unused_imports)]
pub use nostr::NostrClient;
#[allow(unused_imports)]
pub use types::{
    ContractType, Offer, OfferFilters, OfferState, OfferSummary, OfferType, RequestDirection,
    RequestStatus, TradeRequest, TradeRequestSummary,
};
