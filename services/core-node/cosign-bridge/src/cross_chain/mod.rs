// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Cross-Chain Settlement Module
//!
//! Extends the existing bulletin board, cosign session, and Fair-Sign
//! infrastructure to support external-chain settlement without building
//! a parallel discovery, negotiation, or execution stack.
//!
//! ## Design
//!
//! - Cross-chain offers travel as `SpotContract` on the bulletin board
//!   with `schema = "cross_chain_spot_v1"` inside `contract_payload`
//! - Schema dispatch detects cross-chain payloads and routes them to
//!   the cross-chain execution path
//! - Each supported chain has an adapter behind a common state machine
//! - The adapters are: `btc_scriptless_v1`, `eth_htlc_v1`, `tron_htlc_v1`
//!
//! ## Modules
//!
//! - [`types`]: Payload schema, settlement profiles, policy structs
//! - [`state`]: Adapter-neutral cross-chain execution state machine
//! - [`validation`]: External address validation (BTC, ETH, TRON)
//! - [`dispatch`]: Schema sniffing on `SpotContract` payloads

pub mod dispatch;
pub mod eth;
pub mod state;
pub mod types;
pub mod validation;

pub use dispatch::{extract_cross_chain_payload, is_cross_chain_payload};
pub use state::{CrossChainExecutionRecord, CrossChainState};
pub use types::{
    AdapterKind, ConfirmationPolicy, CrossChainSpotV1Payload, ExternalChain, ExternalLeg,
    FeeFundingMode, FeePolicy, FundingOrder, SettlementProfile, TimeoutPolicy, TscLeg,
};
pub use validation::validate_external_address;
