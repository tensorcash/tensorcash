// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Ethereum adapter for cross-chain settlement.
//!
//! Provides:
//! - JSON-RPC client for Ethereum node communication
//! - HTLC calldata encoding (lock, claim, refund, getSwap)
//! - Transaction signing (EIP-1559)
//! - Event log watching for HTLC Locked/Claimed/Refunded events

pub mod htlc;
pub mod rpc;
pub mod signer;
pub mod watcher;
