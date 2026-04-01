// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! HWI-style stdio JSON protocol handler
//!
//! Reads newline-delimited JSON commands from stdin
//! Writes JSON responses to stdout

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{self, BufRead, Write};
use thiserror::Error;

use crate::bulletin_board::{
    BulletinBoardManager, ContractType, DiscussionPost, DiscussionScope, Offer, OfferFilters,
    OfferType,
};
use crate::cross_chain::eth::{
    htlc,
    rpc::EthRpcClient,
    signer::{Eip1559Tx, EthSigningKey},
    watcher,
};
use crate::cross_chain::{
    self,
    dispatch::{extract_cross_chain_payload, is_cross_chain_payload},
    CrossChainSpotV1Payload,
};
use crate::session::SessionManager;
use std::str::FromStr;

fn params_get_str(params: &Value, key: &str, index: usize) -> Option<String> {
    if let Some(value) = params.get(key).and_then(|v| v.as_str()) {
        return Some(value.to_string());
    }

    if let Some(array) = params.as_array() {
        if let Some(value) = array.get(index).and_then(|v| v.as_str()) {
            return Some(value.to_string());
        }
    }

    None
}

fn params_get_opt_str(params: &Value, key: &str, index: usize) -> Option<String> {
    params_get_str(params, key, index)
}

#[derive(Error, Debug)]
pub enum BridgeError {
    /// TODO: Use for config validation
    #[allow(dead_code)]
    #[error("Bad configuration: {0}")]
    BadConfiguration(String),

    /// TODO: Use for WebSocket connection failures
    #[allow(dead_code)]
    #[error("Transport down: {0}")]
    TransportDown(String),

    #[error("Crypto initialization failed: {0}")]
    CryptoInitFailed(String),

    #[error("Invalid command: {0}")]
    InvalidCommand(String),

    #[error("Session not found: {0}")]
    SessionNotFound(String),

    #[error("Invalid parameters: {0}")]
    InvalidParams(String),

    /// Set when a transport-mode Noise AEAD decrypt fails. The cipher state
    /// is irrecoverable — any further send/recv on this session would either
    /// keep failing or, worse, accept garbage. The session is marked poisoned
    /// and its transports dropped. The wallet should render
    /// "session desynchronized, restart ceremony" and stop polling this id.
    #[error("COSIGN_SESSION_DESYNCED: {0}")]
    SessionDesynced(String),

    #[error("IO error: {0}")]
    Io(#[from] io::Error),
}

#[derive(Deserialize)]
struct Request {
    command: String,
    #[serde(default)]
    params: Value,
}

#[derive(Serialize)]
struct Response {
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(flatten)]
    data: Value,
}

#[derive(Serialize)]
struct VersionResponse {
    api_version: u32,
    git_commit: String,
    build_flags: Vec<String>,
    bridge_version: String,
}

#[derive(Serialize)]
struct PingResponse {
    bridge_alive: bool,
    version: String,
    transports: Vec<String>,
    uptime_sec: u64,
    capabilities: Vec<String>,
}

pub async fn run() -> Result<()> {
    log::info!("Cosign bridge starting...");

    // Create session manager
    let mut session_manager = SessionManager::new();

    // Bulletin board manager (created via init_bb command)
    let mut bb_manager: Option<BulletinBoardManager> = None;

    // ETH JSON-RPC clients (created via eth_init / eth_init_secondary)
    let mut eth_client: Option<EthRpcClient> = None;
    let mut eth_client_secondary: Option<EthRpcClient> = None;

    // ETH derivation seed for derived:auto signer resolution.
    // This is a child key (m/44'/60'/0') provided by the wallet at eth_init time.
    // The bridge derives account keys at m/44'/60'/0'/0/{index} from it.
    let mut eth_derivation_seed: Option<[u8; 32]> = None;

    // Read from stdin line by line
    let stdin = io::stdin();
    let mut stdout = io::stdout();

    for line in stdin.lock().lines() {
        let line = line.context("Failed to read stdin")?;

        if line.trim().is_empty() {
            continue;
        }

        // Parse request
        let request: Request = match serde_json::from_str(&line) {
            Ok(req) => req,
            Err(e) => {
                write_error(&mut stdout, &format!("Invalid JSON: {}", e))?;
                continue;
            }
        };

        // Handle command
        let response = handle_command(
            &mut session_manager,
            &mut bb_manager,
            &mut eth_client,
            &mut eth_client_secondary,
            &mut eth_derivation_seed,
            &request.command,
            request.params,
        )
        .await;

        // Write response
        write_response(&mut stdout, response)?;
    }

    log::info!("Cosign bridge shutting down");
    Ok(())
}

async fn handle_command(
    session_manager: &mut SessionManager,
    bb_manager: &mut Option<BulletinBoardManager>,
    eth_client: &mut Option<EthRpcClient>,
    eth_client_secondary: &mut Option<EthRpcClient>,
    eth_derivation_seed: &mut Option<[u8; 32]>,
    command: &str,
    params: Value,
) -> Result<Value, BridgeError> {
    match command {
        // Existing session commands
        "version" => handle_version(),
        "ping" => handle_ping(session_manager),
        "init" => handle_init(session_manager, params).await,
        "join" => handle_join(session_manager, params).await,
        "handshake" => handle_handshake(session_manager, params),
        "handshake_finish" => handle_handshake_finish(session_manager, params),
        "handshake_complete" => handle_handshake_complete(session_manager, params),
        "handshake_auto" => handle_handshake_auto(session_manager, params).await,
        "attest" => handle_attest(session_manager, params),
        "send" => handle_send(session_manager, params).await,
        "recv" => handle_recv(session_manager, params).await,
        "status" => handle_status(session_manager, params),
        "close" => handle_close(session_manager, params).await,
        "resume" => handle_resume(session_manager, params),
        "metrics" => handle_metrics(session_manager),

        // New bulletin board commands
        "init_bb" => handle_init_bb(bb_manager, params).await,
        "post_offer" => handle_post_offer(bb_manager, params).await,
        "post_contract_offer" => handle_post_contract_offer(bb_manager, params).await,
        "list_offers" => handle_list_offers(bb_manager, params).await,
        "request_trade" => handle_request_trade(bb_manager, params).await,
        "list_requests" => handle_list_requests(bb_manager, params).await,
        "accept_request" => handle_accept_request(bb_manager, session_manager, params).await,
        "reject_request" => handle_reject_request(bb_manager, params).await,
        "cancel_request" => handle_cancel_request(bb_manager, params).await,
        "delete_offer" => handle_delete_offer(bb_manager, params).await,

        // Bulletin board info (lightweight, no reinit)
        "bb_get_pubkey" => handle_bb_get_pubkey(bb_manager),

        // Discussion commands
        "post_discussion" | "discussion_post" => handle_post_discussion(bb_manager, params).await,
        "list_discussion" | "discussion_list" => handle_list_discussion(bb_manager, params).await,
        "list_discussion_scopes" | "discussion_scopes" => {
            handle_list_discussion_scopes(bb_manager, params).await
        }
        "force_refresh_discussion" | "discussion_force_refresh" => {
            handle_force_refresh_discussion(bb_manager, params).await
        }

        // Cross-chain commands
        "post_cross_chain_offer" => handle_post_cross_chain_offer(bb_manager, params).await,
        "validate_cross_chain_payload" => handle_validate_cross_chain_payload(params),
        "list_cross_chain_offers" => handle_list_cross_chain_offers(bb_manager, params).await,

        // ETH adapter commands
        "eth_init" => handle_eth_init(eth_client, Some(eth_derivation_seed), params),
        "eth_init_secondary" => handle_eth_init(eth_client_secondary, None, params),
        "eth_lock_htlc" => handle_eth_lock_htlc(eth_client, params).await,
        "eth_claim_htlc" => handle_eth_claim_htlc(eth_client, params).await,
        "eth_refund_htlc" => handle_eth_refund_htlc(eth_client, params).await,
        "eth_get_swap_status" => handle_eth_get_swap_status(eth_client, params).await,
        "eth_resolve_signer" => handle_eth_resolve_signer(eth_derivation_seed, params),
        // Secondary provider status query (dual-provider verification)
        "secondary_eth_get_swap_status" => {
            handle_eth_get_swap_status(eth_client_secondary, params).await
        }
        "eth_verify_attestation" => handle_eth_verify_attestation(params),

        // Governance commands
        "publish_governance" => handle_publish_governance(bb_manager, params).await,
        "list_governance" => handle_list_governance(bb_manager, params).await,
        "get_governance" => handle_get_governance(bb_manager, params).await,
        "force_refresh_governance" => handle_force_refresh_governance(bb_manager, params).await,
        "publish_ballot" => handle_publish_ballot(bb_manager, params).await,
        "list_ballots" => handle_list_ballots(bb_manager, params).await,

        // Private governance commands
        "request_private_proposal" => handle_request_private_proposal(bb_manager, params).await,
        "send_governance_ballot_dm" => handle_send_governance_ballot_dm(bb_manager, params).await,
        "process_governance_dms" => handle_process_governance_dms(bb_manager, params).await,
        "send_proposal_response" => handle_send_proposal_response(bb_manager, params).await,
        "get_private_payload" => handle_get_private_payload(bb_manager, params).await,

        _ => Err(BridgeError::InvalidCommand(command.to_string())),
    }
}

fn handle_version() -> Result<Value, BridgeError> {
    let response = VersionResponse {
        api_version: 1,
        git_commit: env!("CARGO_PKG_VERSION").to_string(),
        build_flags: vec!["noise".to_string(), "spake2".to_string()],
        bridge_version: env!("CARGO_PKG_VERSION").to_string(),
    };
    Ok(serde_json::to_value(response).unwrap())
}

fn handle_ping(manager: &SessionManager) -> Result<Value, BridgeError> {
    let response = PingResponse {
        bridge_alive: true,
        version: env!("CARGO_PKG_VERSION").to_string(),
        transports: vec!["ws".to_string()],
        uptime_sec: manager.uptime_seconds(),
        capabilities: vec![
            "resume".to_string(),
            "send_multi".to_string(),
            "bip322".to_string(),
        ],
    };
    Ok(serde_json::to_value(response).unwrap())
}

async fn handle_init(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.init(params).await
}

async fn handle_join(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.join(params).await
}

fn handle_handshake(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.handshake(params)
}

fn handle_handshake_finish(
    manager: &mut SessionManager,
    params: Value,
) -> Result<Value, BridgeError> {
    manager.handshake_finish(params)
}

fn handle_handshake_complete(
    manager: &mut SessionManager,
    params: Value,
) -> Result<Value, BridgeError> {
    manager.handshake_complete(params)
}

async fn handle_handshake_auto(
    manager: &mut SessionManager,
    params: Value,
) -> Result<Value, BridgeError> {
    manager.handshake_auto(params).await
}

fn handle_attest(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.attest(params)
}

async fn handle_send(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.send(params).await
}

async fn handle_recv(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.recv(params).await
}

fn handle_status(manager: &SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.status(params)
}

async fn handle_close(manager: &mut SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.close(params).await
}

fn handle_resume(manager: &SessionManager, params: Value) -> Result<Value, BridgeError> {
    manager.resume(params)
}

fn handle_metrics(manager: &SessionManager) -> Result<Value, BridgeError> {
    manager.metrics()
}

fn write_response(stdout: &mut io::Stdout, result: Result<Value, BridgeError>) -> Result<()> {
    let response = match result {
        Ok(data) => Response { error: None, data },
        Err(e) => Response {
            error: Some(e.to_string()),
            data: serde_json::json!({}),
        },
    };

    let json = serde_json::to_string(&response)?;
    writeln!(stdout, "{}", json)?;
    stdout.flush()?;
    Ok(())
}

fn write_error(stdout: &mut io::Stdout, message: &str) -> Result<()> {
    let response = Response {
        error: Some(message.to_string()),
        data: serde_json::json!({}),
    };

    let json = serde_json::to_string(&response)?;
    writeln!(stdout, "{}", json)?;
    stdout.flush()?;
    Ok(())
}

// ============================================================================
// Bulletin Board Command Handlers
// ============================================================================

/// Initialize bulletin board (connect to Nostr relays)
async fn handle_init_bb(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    // Parse parameters
    let relays = params
        .get("relays")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_else(|| {
            vec![
                "wss://relay.damus.io".to_string(),
                "wss://nos.lol".to_string(),
                "wss://relay.nostr.band".to_string(),
            ]
        });

    let nostr_key_path = params
        .get("nostr_key_path")
        .and_then(|v| v.as_str())
        .map(String::from);

    // Parse network for chain compartmentalization (main, signet, testnet3, regtest)
    // This is passed from Bitcoin Core via gArgs.GetChainTypeString()
    let network = params
        .get("network")
        .and_then(|v| v.as_str())
        .map(String::from)
        .unwrap_or_else(|| "main".to_string()); // Default to mainnet if not specified

    let relay_source = if params.get("relays").and_then(|v| v.as_array()).is_some() {
        "params.relays"
    } else {
        "builtin_defaults"
    };
    log::info!(
        "Initializing bulletin board for network {} using {}: {:?}",
        network,
        relay_source,
        relays
    );

    // Idempotent: if already initialized for the same network, return existing state
    // without replacing the manager. This prevents multi-wallet init_bb calls from
    // clobbering each other's relay connections and Nostr identity.
    if let Some(existing) = bb_manager.as_ref() {
        if existing.get_network() == network {
            log::info!(
                "Bulletin board already initialized for network {}, returning existing state",
                network
            );
            return Ok(serde_json::json!({
                "success": true,
                "pubkey": existing.get_pubkey(),
                "relays": existing.get_relay_urls(),
                "network": network,
            }));
        }
        log::info!(
            "Bulletin board network changed from {} to {}, reinitializing",
            existing.get_network(),
            network
        );
    }

    // Create bulletin board manager with network for offer compartmentalization
    let manager = BulletinBoardManager::new(relays.clone(), nostr_key_path, network.clone())
        .await
        .map_err(|e| {
            BridgeError::CryptoInitFailed(format!("Failed to initialize bulletin board: {}", e))
        })?;

    let pubkey = manager.get_pubkey();
    let relay_urls = manager.get_relay_urls();

    *bb_manager = Some(manager);

    Ok(serde_json::json!({
        "success": true,
        "pubkey": pubkey,
        "relays": relay_urls,
        "network": network,
    }))
}

/// Post an offer to the bulletin board
async fn handle_post_offer(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Parse offer parameters
    let offer_type = params
        .get("offer_type")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing offer_type".to_string()))?;

    let offer_type = match offer_type {
        "buy" => OfferType::Buy,
        "sell" => OfferType::Sell,
        "swap" => OfferType::Swap,
        _ => {
            return Err(BridgeError::InvalidCommand(format!(
                "Invalid offer_type: {}",
                offer_type
            )))
        }
    };

    let asset_send = params
        .get("asset_send")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing asset_send".to_string()))?
        .to_string();

    let asset_recv = params
        .get("asset_recv")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing asset_recv".to_string()))?
        .to_string();

    let amount = params
        .get("amount")
        .and_then(|v| v.as_f64())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing amount".to_string()))?;

    let price = params
        .get("price")
        .and_then(|v| v.as_f64())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing price".to_string()))?;

    let maker_pubkey = manager.get_pubkey();
    let network = manager.get_network().to_string();

    // Create offer with network for chain compartmentalization
    let mut offer = Offer::new(
        offer_type,
        asset_send,
        asset_recv,
        amount,
        price,
        maker_pubkey,
        network,
    );

    // Optional parameters
    if let Some(payment_methods) = params.get("payment_methods").and_then(|v| v.as_array()) {
        offer.payment_methods = payment_methods
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
    }

    if let Some(regions) = params.get("regions").and_then(|v| v.as_array()) {
        offer.regions = regions
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect();
    }

    if let Some(requires_escrow) = params.get("requires_escrow").and_then(|v| v.as_bool()) {
        offer.requires_escrow = requires_escrow;
    }

    if let Some(min_reputation) = params.get("min_reputation_score").and_then(|v| v.as_f64()) {
        offer.min_reputation_score = min_reputation as f32;
    }

    // Post offer
    let offer_id = manager
        .post_offer(offer)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to post offer: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "offer_id": offer_id,
    }))
}

/// Post a contract offer to the bulletin board
///
/// This takes a full contract JSON payload from repo.propose/forward.propose/spot.propose
/// and extracts/computes the necessary fields for bulletin board posting.
async fn handle_post_contract_offer(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Parse contract type
    let contract_type_str = params
        .get("contract_type")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing contract_type".to_string()))?;

    let contract_type = match contract_type_str {
        "repo" => ContractType::Repo,
        "forward" => ContractType::Forward,
        "spot" => ContractType::Spot,
        "difficulty" => ContractType::Difficulty,
        _ => {
            return Err(BridgeError::InvalidCommand(format!(
                "Invalid contract_type: {}",
                contract_type_str
            )))
        }
    };

    // Get contract payload (base64-encoded or raw JSON)
    let contract_payload = params
        .get("contract_payload")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing contract_payload".to_string()))?
        .to_string();

    // Parse maker role
    let maker_role = params
        .get("maker_role")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            BridgeError::InvalidCommand(
                "Missing maker_role (lender/borrower/long/short)".to_string(),
            )
        })?
        .to_string();

    // Get pre-computed display fields (optional)
    let apr = params.get("apr").and_then(|v| v.as_f64());
    let ltv = params.get("ltv").and_then(|v| v.as_f64());
    let tenor_days = params
        .get("tenor_days")
        .and_then(|v| v.as_u64())
        .map(|u| u as u32);

    // Parse optional proof_of_funds array
    let proof_of_funds = if let Some(proof_array) = params.get("proof_of_funds") {
        if proof_array.is_array() {
            let proofs: Result<Vec<crate::bulletin_board::governance::OwnershipProof>, _> =
                serde_json::from_value(proof_array.clone());
            match proofs {
                Ok(p) if !p.is_empty() => Some(p),
                Ok(_) => None, // Empty array treated as None
                Err(e) => {
                    log::warn!("Failed to parse proof_of_funds: {}", e);
                    None
                }
            }
        } else {
            None
        }
    } else {
        None
    };

    let maker_pubkey = manager.get_pubkey();
    let network = manager.get_network().to_string();

    // Create contract offer with network for chain compartmentalization
    let mut offer = Offer::new_contract(
        contract_type,
        contract_payload,
        maker_role,
        maker_pubkey,
        network,
        apr,
        ltv,
        tenor_days,
    );

    // Attach proof of funds if provided
    offer.proof_of_funds = proof_of_funds;

    // Post offer
    let offer_id = manager
        .post_offer(offer)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to post contract offer: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "offer_id": offer_id,
    }))
}

/// List offers from the bulletin board
async fn handle_list_offers(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Check for force_refresh parameter
    let force_refresh = params
        .get("force_refresh")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    if force_refresh {
        log::info!("Force refreshing offers from Nostr relays");
        manager.force_refresh_offers().await?;
    }

    // Parse filters (network is automatically applied by the manager)
    let filters = OfferFilters {
        network: None, // Network filtering is handled by the manager based on init_bb
        offer_type: params
            .get("offer_type")
            .and_then(|v| v.as_str())
            .map(String::from),
        min_amount: params.get("min_amount").and_then(|v| v.as_f64()),
        max_amount: params.get("max_amount").and_then(|v| v.as_f64()),
        region: params
            .get("region")
            .and_then(|v| v.as_str())
            .map(String::from),
        payment_method: params
            .get("payment_method")
            .and_then(|v| v.as_str())
            .map(String::from),
        min_reputation: params
            .get("min_reputation")
            .and_then(|v| v.as_f64())
            .map(|f| f as f32),
        contract_type: params
            .get("contract_type")
            .and_then(|v| v.as_str())
            .map(String::from),
        maker_role: params
            .get("maker_role")
            .and_then(|v| v.as_str())
            .map(String::from),
        min_apr: params.get("min_apr").and_then(|v| v.as_f64()),
        max_apr: params.get("max_apr").and_then(|v| v.as_f64()),
        min_tenor_days: params
            .get("min_tenor_days")
            .and_then(|v| v.as_u64())
            .map(|u| u as u32),
        max_tenor_days: params
            .get("max_tenor_days")
            .and_then(|v| v.as_u64())
            .map(|u| u as u32),
    };

    // List offers
    let offers = manager
        .list_offers(filters)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list offers: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "offers": offers,
    }))
}

/// Request to trade on an offer (taker)
async fn handle_request_trade(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let offer_id = params
        .get("offer_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing offer_id".to_string()))?;

    let message = params
        .get("message")
        .and_then(|v| v.as_str())
        .map(String::from);

    // Parse optional proof_of_funds array
    let proof_of_funds = if let Some(proof_array) = params.get("proof_of_funds") {
        if proof_array.is_array() {
            let proofs: Result<Vec<crate::bulletin_board::governance::OwnershipProof>, _> =
                serde_json::from_value(proof_array.clone());
            match proofs {
                Ok(p) if !p.is_empty() => Some(p),
                Ok(_) => None, // Empty array treated as None
                Err(e) => {
                    log::warn!("Failed to parse proof_of_funds: {}", e);
                    None
                }
            }
        } else {
            None
        }
    } else {
        None
    };

    let taker_pubkey = manager.get_pubkey();

    // Request trade
    let request_id = manager
        .request_trade(offer_id, &taker_pubkey, message, proof_of_funds)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to request trade: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "request_id": request_id,
    }))
}

/// List pending trade requests (maker)
async fn handle_list_requests(
    bb_manager: &mut Option<BulletinBoardManager>,
    _params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let user_pubkey = manager.get_pubkey();

    // List requests
    let requests = manager
        .list_trade_requests(&user_pubkey)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list requests: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "requests": requests,
    }))
}

/// Accept a trade request (maker creates session and sends invite link)
async fn handle_accept_request(
    bb_manager: &mut Option<BulletinBoardManager>,
    session_manager: &mut SessionManager,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let request_id = params_get_str(&params, "request_id", 0)
        .ok_or_else(|| BridgeError::InvalidCommand("Missing request_id".to_string()))?;

    // Parse session parameters (transport, ttl, relay_url)
    let transport = params
        .get("transport")
        .and_then(|v| v.as_str())
        .unwrap_or("websocket");

    let ttl = params
        .get("ttl")
        .and_then(|v| v.as_u64())
        .map(|t| t as u32)
        .unwrap_or(1800); // 30 minutes default

    // Get relay_url from params or environment variable
    // Priority: 1) params, 2) COSIGN_RELAY_URL env, 3) None (will use default in session.rs)
    let relay_url = params
        .get("relay_url")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .or_else(|| std::env::var("COSIGN_RELAY_URL").ok());

    // Create a new session for this trade
    let mut session_params_obj = serde_json::json!({
        "transport": transport,
        "ttl": ttl,
    });

    // Add relay_url if provided
    if let Some(url) = relay_url {
        session_params_obj["relay_url"] = serde_json::Value::String(url);
    }

    let session_params = session_params_obj;

    let session_result = handle_init(session_manager, session_params).await?;

    // Extract invite link from session init response
    let invite_link = session_result
        .get("invite_link")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            BridgeError::CryptoInitFailed("Failed to create session invite link".to_string())
        })?
        .to_string();

    // Extract session_id from session init response
    let session_id = session_result
        .get("session_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            BridgeError::CryptoInitFailed("Failed to get session_id from init".to_string())
        })?
        .to_string();

    // Extract transport info from session result
    let transport_str = session_result
        .get("transport")
        .and_then(|v| v.as_str())
        .unwrap_or("websocket");

    let relay_url = session_result
        .get("relay_url")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    // NOTE: We do NOT extract SAS here because it's meaningless before handshake completes.
    // The SAS is calculated using different session_ids on each side at this point.
    // Only after handshake_auto completes will both parties have matching SAS derived from handshake_hash.

    // Accept the request (sends invite link to taker via DM)
    manager
        .accept_trade_request(&request_id, invite_link.clone())
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to accept request: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "invite_link": invite_link,
        "session_id": session_id,
        "transport": transport_str,
        "relay_url": relay_url,
    }))
}

/// Reject a trade request (maker)
async fn handle_reject_request(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let request_id = params_get_str(&params, "request_id", 0)
        .ok_or_else(|| BridgeError::InvalidCommand("Missing request_id".to_string()))?;

    let reason = params_get_opt_str(&params, "reason", 1);

    // Reject the request
    manager
        .reject_trade_request(&request_id, reason)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to reject request: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
    }))
}

/// Cancel a trade request (taker)
async fn handle_cancel_request(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let request_id = params_get_str(&params, "request_id", 0)
        .ok_or_else(|| BridgeError::InvalidCommand("Missing request_id".to_string()))?;

    let reason = params_get_opt_str(&params, "reason", 1);

    manager
        .cancel_trade_request(&request_id, reason)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to cancel request: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
    }))
}

/// Delete an offer (maker)
async fn handle_delete_offer(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let offer_id = params
        .get("offer_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing offer_id".to_string()))?;

    // Delete the offer
    manager
        .delete_offer(offer_id)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to delete offer: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
    }))
}

// ============================================================================
// BULLETIN BOARD INFO
// ============================================================================

fn handle_bb_get_pubkey(
    bb_manager: &mut Option<BulletinBoardManager>,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_ref().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    Ok(serde_json::json!({
        "pubkey": manager.get_pubkey(),
        "network": manager.get_network(),
    }))
}

// ============================================================================
// DISCUSSION COMMAND HANDLERS
// ============================================================================

async fn handle_post_discussion(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let scope_type_str = params
        .get("scope_type")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing scope_type".to_string()))?;
    let scope_type = DiscussionScope::from_str(scope_type_str)
        .map_err(|e| BridgeError::InvalidCommand(e.to_string()))?;

    let scope_id = params
        .get("scope_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing scope_id".to_string()))?
        .to_string();

    let content = params
        .get("content")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing content".to_string()))?
        .to_string();

    let model_identifier = params
        .get("model_identifier")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let proof_value = params
        .get("proof")
        .or_else(|| params.get("ownership_proof"));
    let proof = if let Some(value) = proof_value {
        Some(serde_json::from_value(value.clone()).map_err(|e| {
            BridgeError::InvalidCommand(format!("Invalid discussion proof format: {}", e))
        })?)
    } else {
        None
    };

    let post = DiscussionPost::new(
        scope_type,
        scope_id,
        manager.get_network().to_string(),
        manager.get_pubkey(),
        content,
        model_identifier,
        proof,
    )
    .map_err(BridgeError::InvalidCommand)?;

    let published = manager
        .post_discussion(post)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to post discussion: {}", e)))?;

    Ok(serde_json::json!({
        "success": true,
        "post": published,
    }))
}

async fn handle_list_discussion(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let scope_type = params
        .get("scope_type")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing scope_type".to_string()))?
        .to_string();

    let scope_id = params
        .get("scope_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing scope_id".to_string()))?
        .to_string();

    let since = params.get("since").and_then(|v| v.as_u64());
    let limit = params
        .get("limit")
        .and_then(|v| v.as_u64())
        .map(|v| v as usize);

    let discussion = manager
        .list_discussion(scope_type, scope_id, since, limit)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list discussion: {}", e)))?;

    Ok(serde_json::json!({
        "posts": discussion.posts,
        "stale": discussion.stale,
        "refresh_error": discussion.refresh_error,
    }))
}

async fn handle_list_discussion_scopes(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let since = params.get("since").and_then(|v| v.as_u64());
    let limit = params
        .get("limit")
        .and_then(|v| v.as_u64())
        .map(|v| v as usize);
    let force_refresh = params
        .get("force_refresh")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    let scopes = manager
        .list_discussion_scopes(since, limit, force_refresh)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list discussion scopes: {}", e)))?;

    Ok(serde_json::json!({
        "scopes": scopes,
    }))
}

async fn handle_force_refresh_discussion(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let scope_type = params
        .get("scope_type")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing scope_type".to_string()))?
        .to_string();

    let scope_id = params
        .get("scope_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing scope_id".to_string()))?
        .to_string();

    let since = params.get("since").and_then(|v| v.as_u64());

    manager
        .force_refresh_discussion(scope_type, scope_id, since)
        .await
        .map_err(|e| {
            BridgeError::TransportDown(format!("Failed to force refresh discussion: {}", e))
        })?;

    Ok(serde_json::json!({
        "success": true,
    }))
}

// ============================================================================
// GOVERNANCE COMMAND HANDLERS
// ============================================================================

async fn handle_publish_governance(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Parse proposal from params
    let proposal_json = params
        .get("proposal")
        .ok_or_else(|| BridgeError::InvalidCommand("Missing proposal".to_string()))?;

    let proposal: crate::bulletin_board::governance::GovernanceProposal =
        serde_json::from_value(proposal_json.clone())
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid proposal format: {}", e)))?;

    // Parse optional rate limit
    let rate_limit_secs = params.get("rate_limit_secs").and_then(|v| v.as_u64());

    // Publish the proposal
    let proposal_id = manager
        .publish_governance(proposal, rate_limit_secs)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to publish governance: {}", e)))?;

    Ok(serde_json::json!({
        "proposal_id": proposal_id,
    }))
}

async fn handle_list_governance(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Parse filters
    let asset_id = params
        .get("asset_id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let include_expired = params
        .get("include_expired")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    // List proposals
    let proposals = manager
        .list_governance(asset_id, include_expired)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list governance: {}", e)))?;

    // Wrap array in object for Response struct (which uses #[serde(flatten)])
    Ok(serde_json::json!({
        "proposals": proposals
    }))
}

async fn handle_get_governance(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let proposal_id = params
        .get("proposal_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing proposal_id".to_string()))?;

    // Get the proposal
    let proposal = manager
        .get_governance(proposal_id)
        .await
        .map_err(|e| BridgeError::SessionNotFound(format!("Proposal not found: {}", e)))?;

    serde_json::to_value(proposal)
        .map_err(|e| BridgeError::InvalidCommand(format!("Failed to serialize proposal: {}", e)))
}

async fn handle_force_refresh_governance(
    bb_manager: &mut Option<BulletinBoardManager>,
    _params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Force refresh from Nostr (bypasses 5-minute cache)
    manager.force_refresh_governance().await.map_err(|e| {
        BridgeError::TransportDown(format!("Failed to force refresh governance: {}", e))
    })?;

    Ok(serde_json::json!({"success": true}))
}

async fn handle_publish_ballot(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Parse ballot from params
    let ballot_json = params
        .get("ballot")
        .ok_or_else(|| BridgeError::InvalidCommand("Missing ballot".to_string()))?;

    let ballot: crate::bulletin_board::governance::GovernanceBallot =
        serde_json::from_value(ballot_json.clone())
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid ballot format: {}", e)))?;

    // Publish the ballot
    let ballot_id = manager
        .publish_ballot(ballot)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to publish ballot: {}", e)))?;

    Ok(serde_json::json!({
        "ballot_id": ballot_id,
    }))
}

async fn handle_list_ballots(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let proposal_id = params
        .get("proposal_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing proposal_id".to_string()))?;

    // List ballots for the proposal
    let ballots = manager
        .list_ballots(proposal_id.to_string())
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list ballots: {}", e)))?;

    // Return ballots array
    Ok(serde_json::json!({
        "ballots": ballots
    }))
}

// ==================== PRIVATE GOVERNANCE HANDLERS ====================

async fn handle_request_private_proposal(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    log::info!(
        "DEBUG: handle_request_private_proposal called with params: {:?}",
        params
    );

    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Extract parameters
    let proposal_id = params
        .get("proposal_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing proposal_id".to_string()))?
        .to_string();

    let asset_id = params
        .get("asset_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing asset_id".to_string()))?
        .to_string();

    let issuer_nostr_pubkey = params
        .get("issuer_nostr_pubkey")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing issuer_nostr_pubkey".to_string()))?
        .to_string();

    let holder_nostr_pubkey = params
        .get("holder_nostr_pubkey")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing holder_nostr_pubkey".to_string()))?
        .to_string();

    let ownership_proof = params
        .get("ownership_proof")
        .ok_or_else(|| BridgeError::InvalidCommand("Missing ownership_proof".to_string()))?
        .clone();

    // Parse ownership proof
    let proof_obj: crate::bulletin_board::governance::OwnershipProof =
        serde_json::from_value(ownership_proof).map_err(|e| {
            BridgeError::InvalidCommand(format!("Invalid ownership_proof format: {}", e))
        })?;

    // Construct GovernanceAccessRequest using constructor
    let request = crate::bulletin_board::governance::GovernanceAccessRequest::new(
        proposal_id.clone(),
        asset_id,
        holder_nostr_pubkey,
        proof_obj,
    );

    // Send request
    let request_id = manager
        .request_private_proposal(request, issuer_nostr_pubkey)
        .await
        .map_err(|e| {
            BridgeError::TransportDown(format!("Failed to request private proposal: {}", e))
        })?;

    Ok(serde_json::json!({
        "session_id": request_id,
        "status": "sent",
        "message": "Access request sent to issuer via encrypted DM"
    }))
}

async fn handle_send_governance_ballot_dm(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Extract parameters
    let proposal_id = params
        .get("proposal_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing proposal_id".to_string()))?
        .to_string();

    let asset_id = params
        .get("asset_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing asset_id".to_string()))?
        .to_string();

    let signed_psbt = params
        .get("signed_psbt")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing signed_psbt".to_string()))?
        .to_string();

    let ballot_units = params
        .get("ballot_units")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing ballot_units".to_string()))?;

    // Construct GovernanceBallotDM
    let ballot_dm = crate::bulletin_board::governance::GovernanceBallotDM {
        version: 1,
        proposal_id: proposal_id.clone(),
        asset_id,
        signed_psbt,
        ballot_units,
        ballot_timestamp: chrono::Utc::now().timestamp() as u64,
        sequence: Some(1), // TODO: Implement proper sequence tracking
    };

    // Send ballot via DM
    let ballot_id = manager
        .send_governance_ballot_dm(ballot_dm)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to send ballot DM: {}", e)))?;

    Ok(serde_json::json!({
        "ballot_id": ballot_id,
        "units_accepted": ballot_units,
        "status": "sent",
        "quorum_status": serde_json::Value::Null  // Will be updated when receipt is received
    }))
}

async fn handle_process_governance_dms(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Extract optional 'since' timestamp
    let since = params.get("since").and_then(|v| v.as_u64());

    // Process DMs and get parsed messages
    let (access_requests, proposal_responses, ballot_dms, ballot_receipts) =
        manager.process_governance_dms(since).await.map_err(|e| {
            BridgeError::TransportDown(format!("Failed to process governance DMs: {}", e))
        })?;

    // Convert to JSON response
    let access_reqs_json: Vec<serde_json::Value> = access_requests
        .into_iter()
        .map(|(req, from_pubkey)| {
            serde_json::json!({
                "proposal_id": req.proposal_id,
                "asset_id": req.asset_id,
                "holder_nostr_pubkey": req.holder_nostr_pubkey,
                "ownership_proof": {
                    "utxo_ref": req.ownership_proof.utxo_ref,
                    "address": req.ownership_proof.address,
                    "message": req.ownership_proof.message,
                    "signature": req.ownership_proof.signature,
                    "asset_units": req.ownership_proof.asset_units,
                },
                "requested_at": req.requested_at,
                "from_pubkey": from_pubkey,
            })
        })
        .collect();

    let proposal_resps_json: Vec<serde_json::Value> = proposal_responses
        .into_iter()
        .map(|(resp, from_pubkey)| {
            serde_json::json!({
                "proposal_id": resp.proposal_id,
                "icu_text": resp.icu_text,
                "canonical_icu_hash": resp.canonical_icu_hash,
                "witness_bundle": resp.witness_bundle,
                "witness_bundle_hash": resp.witness_bundle_hash,
                "template_psbt": resp.template_psbt,
                "template_psbt_hash": resp.template_psbt_hash,
                "responded_at": resp.responded_at,
                "from_pubkey": from_pubkey,
            })
        })
        .collect();

    let ballot_dms_json: Vec<serde_json::Value> = ballot_dms
        .into_iter()
        .map(|(ballot, from_pubkey)| {
            serde_json::json!({
                "proposal_id": ballot.proposal_id,
                "asset_id": ballot.asset_id,
                "signed_psbt": ballot.signed_psbt,
                "ballot_units": ballot.ballot_units,
                "ballot_timestamp": ballot.ballot_timestamp,
                "from_pubkey": from_pubkey,
            })
        })
        .collect();

    let receipts_json: Vec<serde_json::Value> = ballot_receipts
        .into_iter()
        .map(|(receipt, from_pubkey)| {
            let quorum_status = receipt.quorum_status.map(|qs| {
                serde_json::json!({
                    "total_voted_units": qs.total_voted_units,
                    "settled_supply": qs.settled_supply,
                    "quorum_bps": qs.quorum_bps,
                    "quorum_reached": qs.quorum_reached,
                })
            });

            serde_json::json!({
                "proposal_id": receipt.proposal_id,
                "ballot_id": receipt.ballot_id,
                "units_accepted": receipt.units_accepted,
                "receipt_timestamp": receipt.receipt_timestamp,
                "quorum_status": quorum_status,
                "from_pubkey": from_pubkey,
            })
        })
        .collect();

    Ok(serde_json::json!({
        "access_requests": access_reqs_json,
        "proposal_responses": proposal_resps_json,
        "ballot_dms": ballot_dms_json,
        "ballot_receipts": receipts_json,
    }))
}

async fn handle_send_proposal_response(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    use crate::bulletin_board::governance::GovernanceProposalResponse;

    let manager = bb_manager
        .as_mut()
        .ok_or_else(|| BridgeError::InvalidCommand("Bulletin board not initialized".to_string()))?;

    // Extract parameters
    let holder_pubkey = params
        .get("holder_nostr_pubkey")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing holder_nostr_pubkey".to_string()))?;

    let response_data = params
        .get("response")
        .ok_or_else(|| BridgeError::InvalidCommand("Missing response data".to_string()))?;

    // Parse response data into GovernanceProposalResponse
    let proposal_response: GovernanceProposalResponse =
        serde_json::from_value(response_data.clone())
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid response format: {}", e)))?;

    // Send the proposal response via DM
    let event_id = manager
        .send_proposal_response(holder_pubkey, proposal_response)
        .await
        .map_err(|e| {
            BridgeError::TransportDown(format!("Failed to send proposal response: {}", e))
        })?;

    Ok(serde_json::json!({
        "success": true,
        "event_id": event_id,
    }))
}

async fn handle_get_private_payload(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager
        .as_mut()
        .ok_or_else(|| BridgeError::InvalidCommand("Bulletin board not initialized".to_string()))?;

    // Extract proposal_id
    let proposal_id = params
        .get("proposal_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing proposal_id".to_string()))?
        .to_string();

    // Retrieve from private payloads cache
    let payload_data = manager.get_private_payload(&proposal_id).await?;

    Ok(serde_json::to_value(payload_data).unwrap())
}

// ---------------------------------------------------------------------------
// Cross-chain handlers
// ---------------------------------------------------------------------------

/// Post a cross-chain offer to the bulletin board.
///
/// Validates the `cross_chain_spot_v1` payload (including external addresses,
/// timeout gaps, confirmation thresholds, and adapter/chain consistency),
/// then posts it as a `SpotContract` offer through the existing board path.
///
/// Required params:
/// - `contract_payload`: JSON string or base64 of the cross-chain payload
/// - `maker_role`: optional, defaults to payload's `role` field
///
/// Optional params:
/// - `proof_of_funds`: array of BIP-322 ownership proofs
async fn handle_post_cross_chain_offer(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    // Get contract payload (raw JSON string or base64)
    let payload_str = params
        .get("contract_payload")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing contract_payload".to_string()))?;

    // Decode base64 if needed, then parse
    let json_str = decode_payload_str(payload_str);
    let payload: CrossChainSpotV1Payload = serde_json::from_str(&json_str).map_err(|e| {
        BridgeError::InvalidCommand(format!("Failed to parse cross-chain payload: {}", e))
    })?;

    // Validate the full payload (addresses, timeouts, adapter consistency, etc.)
    payload.validate().map_err(|e| {
        BridgeError::InvalidCommand(format!("Cross-chain payload validation failed: {}", e))
    })?;

    // Posting an offer means you are the maker. The payload must agree.
    if payload.role != "maker" {
        return Err(BridgeError::InvalidCommand(format!(
            "post_cross_chain_offer requires payload.role == \"maker\", got \"{}\"",
            payload.role
        )));
    }

    // Board-level maker_role is derived from the payload — no caller override
    let maker_role = payload.role.clone();

    // Parse optional proof_of_funds
    let proof_of_funds = parse_proof_of_funds(&params);

    let maker_pubkey = manager.get_pubkey();
    let network = manager.get_network().to_string();

    // Post as SpotContract through the existing path
    let mut offer = Offer::new_contract(
        ContractType::Spot,
        json_str, // store the raw JSON (not re-serialized) to preserve field order
        maker_role,
        maker_pubkey,
        network,
        None, // apr — not applicable for cross-chain spot
        None, // ltv — not applicable
        None, // tenor_days — not applicable
    );

    offer.proof_of_funds = proof_of_funds;

    let offer_id = manager.post_offer(offer).await.map_err(|e| {
        BridgeError::TransportDown(format!("Failed to post cross-chain offer: {}", e))
    })?;

    Ok(serde_json::json!({
        "success": true,
        "offer_id": offer_id,
        "schema": cross_chain::types::CROSS_CHAIN_SPOT_V1_SCHEMA,
        "external_chain": payload.external_leg.chain,
        "adapter": payload.external_leg.adapter,
        "funding_order": payload.funding_order,
    }))
}

/// Validate a cross-chain payload without posting it.
///
/// Returns validation result with detailed error if invalid.
/// Useful for pre-flight checks in Qt before the user submits.
fn handle_validate_cross_chain_payload(params: Value) -> Result<Value, BridgeError> {
    let payload_str = params
        .get("contract_payload")
        .and_then(|v| v.as_str())
        .ok_or_else(|| BridgeError::InvalidCommand("Missing contract_payload".to_string()))?;

    let json_str = decode_payload_str(payload_str);

    // Parse
    let payload: CrossChainSpotV1Payload = match serde_json::from_str(&json_str) {
        Ok(p) => p,
        Err(e) => {
            return Ok(serde_json::json!({
                "valid": false,
                "error": format!("JSON parse error: {}", e),
            }));
        }
    };

    // Validate
    match payload.validate() {
        Ok(()) => Ok(serde_json::json!({
            "valid": true,
            "schema": payload.schema,
            "external_chain": payload.external_leg.chain,
            "adapter": payload.external_leg.adapter,
            "funding_order": payload.funding_order,
            "tsc_units": payload.tsc_leg.units,
            "external_units": payload.external_leg.units,
        })),
        Err(e) => Ok(serde_json::json!({
            "valid": false,
            "error": e,
        })),
    }
}

/// List cross-chain offers from the bulletin board.
///
/// Filters for `SpotContract` offers whose `contract_payload` contains
/// `schema == "cross_chain_spot_v1"`, then returns them with the parsed
/// payload attached for convenience.
///
/// Optional params:
/// - `force_refresh`: bool — re-fetch from Nostr relays first
/// - `external_chain`: string — filter by external chain ("btc", "ethereum", "tron")
/// - `adapter`: string — filter by adapter kind
async fn handle_list_cross_chain_offers(
    bb_manager: &mut Option<BulletinBoardManager>,
    params: Value,
) -> Result<Value, BridgeError> {
    let manager = bb_manager.as_mut().ok_or_else(|| {
        BridgeError::InvalidCommand(
            "Bulletin board not initialized (call init_bb first)".to_string(),
        )
    })?;

    let force_refresh = params
        .get("force_refresh")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if force_refresh {
        manager.force_refresh_offers().await?;
    }

    // Fetch all SpotContract offers
    let filters = OfferFilters {
        contract_type: Some("spot".to_string()),
        ..Default::default()
    };

    let offers = manager
        .list_offers(filters)
        .await
        .map_err(|e| BridgeError::TransportDown(format!("Failed to list offers: {}", e)))?;

    // Optional filters from params
    let chain_filter = params.get("external_chain").and_then(|v| v.as_str());
    let adapter_filter = params.get("adapter").and_then(|v| v.as_str());

    // Filter to cross-chain only, parse payloads, apply filters
    let mut cross_chain_offers = Vec::new();

    for offer in &offers {
        if !is_cross_chain_payload(offer) {
            continue;
        }

        // Parse the payload for filtering and response enrichment
        let payload = match extract_cross_chain_payload(offer) {
            Ok(p) => p,
            Err(_) => continue, // skip malformed cross-chain payloads
        };

        // Apply chain filter
        if let Some(chain) = chain_filter {
            let offer_chain = payload.external_leg.chain.to_string();
            if offer_chain != chain {
                continue;
            }
        }

        // Apply adapter filter
        if let Some(adapter) = adapter_filter {
            let offer_adapter = serde_json::to_value(&payload.external_leg.adapter)
                .ok()
                .and_then(|v| v.as_str().map(String::from));
            if offer_adapter.as_deref() != Some(adapter) {
                continue;
            }
        }

        cross_chain_offers.push(serde_json::json!({
            "offer_id": offer.id,
            "maker_pubkey": offer.maker_pubkey,
            "state": offer.state,
            "created_at": offer.created_at,
            "expires_at": offer.expires_at,
            "network": offer.network,
            "cross_chain_payload": payload,
        }));
    }

    Ok(serde_json::json!({
        "success": true,
        "count": cross_chain_offers.len(),
        "offers": cross_chain_offers,
    }))
}

/// Decode a payload string: try base64 first, fall back to raw JSON.
fn decode_payload_str(s: &str) -> String {
    use base64::Engine;
    match base64::engine::general_purpose::STANDARD.decode(s) {
        Ok(bytes) => String::from_utf8(bytes).unwrap_or_else(|_| s.to_string()),
        Err(_) => s.to_string(),
    }
}

/// Parse optional proof_of_funds from params.
fn parse_proof_of_funds(
    params: &Value,
) -> Option<Vec<crate::bulletin_board::governance::OwnershipProof>> {
    let proof_array = params.get("proof_of_funds")?;
    if !proof_array.is_array() {
        return None;
    }
    match serde_json::from_value::<Vec<crate::bulletin_board::governance::OwnershipProof>>(
        proof_array.clone(),
    ) {
        Ok(p) if !p.is_empty() => Some(p),
        Ok(_) => None,
        Err(e) => {
            log::warn!("Failed to parse proof_of_funds: {}", e);
            None
        }
    }
}

// ============================================================================
// ETH adapter command handlers
// ============================================================================

/// Initialize the ETH JSON-RPC client.
///
/// Params: { "rpc_url": "http://..." }
fn handle_eth_init(
    eth_client: &mut Option<EthRpcClient>,
    seed_target: Option<&mut Option<[u8; 32]>>,
    params: Value,
) -> Result<Value, BridgeError> {
    let rpc_url = params_get_str(&params, "rpc_url", 0)
        .ok_or_else(|| BridgeError::InvalidParams("rpc_url required".into()))?;

    *eth_client = Some(EthRpcClient::new(&rpc_url));

    // Optional: derivation seed for derived:auto signer resolution
    let mut has_seed = false;
    if let Some(target) = seed_target {
        if let Some(seed_hex) = params_get_opt_str(&params, "derivation_seed", 1) {
            let clean = seed_hex.strip_prefix("0x").unwrap_or(&seed_hex);
            if clean.len() == 64 {
                if let Ok(bytes) = hex::decode(clean) {
                    let mut seed = [0u8; 32];
                    seed.copy_from_slice(&bytes);
                    *target = Some(seed);
                    has_seed = true;
                    log::info!("ETH derivation seed configured for derived:auto");
                }
            }
        }
    }

    log::info!(
        "ETH RPC client initialized: {} (derivation_seed={})",
        rpc_url,
        has_seed
    );

    Ok(serde_json::json!({
        "success": true,
        "rpc_url": rpc_url,
        "has_derivation_seed": has_seed,
    }))
}

/// Helper: require ETH client is initialized.
fn require_eth_client(eth_client: &Option<EthRpcClient>) -> Result<&EthRpcClient, BridgeError> {
    eth_client.as_ref().ok_or_else(|| {
        BridgeError::InvalidParams("ETH client not initialized — call eth_init first".into())
    })
}

/// Helper: parse a hex string to fixed-size byte array.
fn parse_hex_bytes<const N: usize>(hex_str: &str) -> Result<[u8; N], BridgeError> {
    let s = hex_str.strip_prefix("0x").unwrap_or(hex_str);
    let bytes =
        hex::decode(s).map_err(|e| BridgeError::InvalidParams(format!("invalid hex: {}", e)))?;
    if bytes.len() != N {
        return Err(BridgeError::InvalidParams(format!(
            "expected {} bytes, got {}",
            N,
            bytes.len()
        )));
    }
    let mut out = [0u8; N];
    out.copy_from_slice(&bytes);
    Ok(out)
}

/// Lock ETH or ERC-20 tokens into the HTLC contract.
///
/// Params: {
///   "htlc_address": "0x...",       // HTLC contract address
///   "swap_id": "0x...",            // 32-byte swap ID
///   "recipient": "0x...",          // 20-byte recipient address
///   "secret_hash": "0x...",        // sha256(secret), 32 bytes
///   "timelock": 1234567890,        // unix timestamp
///   "amount_wei": "0x...",         // value in wei (hex)
///   "signing_key": "0x...",        // 32-byte private key
///   "token_address": "0x..." | null, // ERC-20 or null for native ETH
///   "gas_limit": 200000,           // optional
///   "max_fee_gwei": 50,            // optional
///   "max_priority_fee_gwei": 2     // optional
/// }
async fn handle_eth_lock_htlc(
    eth_client: &Option<EthRpcClient>,
    params: Value,
) -> Result<Value, BridgeError> {
    let rpc = require_eth_client(eth_client)?;

    let htlc_addr: [u8; 20] = parse_hex_bytes(
        &params_get_str(&params, "htlc_address", 0)
            .ok_or_else(|| BridgeError::InvalidParams("htlc_address required".into()))?,
    )?;
    let swap_id: [u8; 32] = parse_hex_bytes(
        &params_get_str(&params, "swap_id", 1)
            .ok_or_else(|| BridgeError::InvalidParams("swap_id required".into()))?,
    )?;
    let recipient: [u8; 20] = parse_hex_bytes(
        &params_get_str(&params, "recipient", 2)
            .ok_or_else(|| BridgeError::InvalidParams("recipient required".into()))?,
    )?;
    let secret_hash: [u8; 32] = parse_hex_bytes(
        &params_get_str(&params, "secret_hash", 3)
            .ok_or_else(|| BridgeError::InvalidParams("secret_hash required".into()))?,
    )?;
    let timelock = params
        .get("timelock")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| BridgeError::InvalidParams("timelock required".into()))?;
    let amount_wei_hex = params_get_str(&params, "amount_wei", 5)
        .ok_or_else(|| BridgeError::InvalidParams("amount_wei required".into()))?;
    let signing_key_hex = params_get_str(&params, "signing_key", 6)
        .ok_or_else(|| BridgeError::InvalidParams("signing_key required".into()))?;

    let key_bytes: [u8; 32] = parse_hex_bytes(&signing_key_hex)?;
    let signer = EthSigningKey::from_bytes(key_bytes);
    let from_address = signer.address();

    let token_address = params_get_opt_str(&params, "token_address", 7);
    let is_native = token_address.is_none()
        || token_address.as_deref() == Some("")
        || token_address.as_deref() == Some("0x0000000000000000000000000000000000000000");

    // Build calldata
    let calldata = if is_native {
        htlc::encode_lock(&swap_id, &recipient, &secret_hash, timelock)
    } else {
        let token_addr: [u8; 20] = parse_hex_bytes(token_address.as_deref().unwrap())?;
        let mut amount_bytes = [0u8; 32];
        let amt_hex = amount_wei_hex.strip_prefix("0x").unwrap_or(&amount_wei_hex);
        let amt_decoded = hex::decode(amt_hex)
            .map_err(|e| BridgeError::InvalidParams(format!("amount_wei hex: {}", e)))?;
        let start = 32 - amt_decoded.len();
        amount_bytes[start..].copy_from_slice(&amt_decoded);
        htlc::encode_lock_token(
            &swap_id,
            &recipient,
            &token_addr,
            &amount_bytes,
            &secret_hash,
            timelock,
        )
    };

    // Parse value (only for native ETH)
    let mut value = [0u8; 32];
    if is_native {
        let v_hex = amount_wei_hex.strip_prefix("0x").unwrap_or(&amount_wei_hex);
        let v_decoded = hex::decode(v_hex)
            .map_err(|e| BridgeError::InvalidParams(format!("amount_wei hex: {}", e)))?;
        let start = 32 - v_decoded.len();
        value[start..].copy_from_slice(&v_decoded);
    }

    // Get chain parameters
    let chain_id = rpc
        .chain_id()
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_chainId: {}", e)))?;
    let nonce = rpc
        .get_nonce(&format!("0x{}", hex::encode(from_address)))
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_getTransactionCount: {}", e)))?;

    let gas_limit = params
        .get("gas_limit")
        .and_then(|v| v.as_u64())
        .unwrap_or(200_000);
    let max_priority = params
        .get("max_priority_fee_gwei")
        .and_then(|v| v.as_u64())
        .unwrap_or(2)
        * 1_000_000_000;
    let max_fee = params
        .get("max_fee_gwei")
        .and_then(|v| v.as_u64())
        .unwrap_or(50)
        * 1_000_000_000;

    let tx = Eip1559Tx {
        chain_id,
        nonce,
        max_priority_fee_per_gas: max_priority,
        max_fee_per_gas: max_fee,
        gas_limit,
        to: htlc_addr,
        value,
        data: calldata,
    };

    let signed = signer.sign_tx(&tx);
    let raw_hex = format!("0x{}", hex::encode(&signed.raw));
    let tx_hash_hex = format!("0x{}", hex::encode(signed.hash));

    // Broadcast
    let broadcast_hash = rpc
        .send_raw_tx(&raw_hex)
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_sendRawTransaction: {}", e)))?;

    log::info!("ETH HTLC lock broadcast: tx_hash={}", broadcast_hash);

    Ok(serde_json::json!({
        "success": true,
        "tx_hash": broadcast_hash,
        "local_hash": tx_hash_hex,
        "from": format!("0x{}", hex::encode(from_address)),
        "nonce": nonce,
    }))
}

/// Claim locked funds by revealing the secret.
///
/// Params: {
///   "htlc_address": "0x...",
///   "swap_id": "0x...",
///   "secret": "0x...",           // 32-byte preimage
///   "signing_key": "0x...",
///   "gas_limit": 100000,         // optional
///   "max_fee_gwei": 50,          // optional
///   "max_priority_fee_gwei": 2   // optional
/// }
async fn handle_eth_claim_htlc(
    eth_client: &Option<EthRpcClient>,
    params: Value,
) -> Result<Value, BridgeError> {
    let rpc = require_eth_client(eth_client)?;

    let htlc_addr: [u8; 20] = parse_hex_bytes(
        &params_get_str(&params, "htlc_address", 0)
            .ok_or_else(|| BridgeError::InvalidParams("htlc_address required".into()))?,
    )?;
    let swap_id: [u8; 32] = parse_hex_bytes(
        &params_get_str(&params, "swap_id", 1)
            .ok_or_else(|| BridgeError::InvalidParams("swap_id required".into()))?,
    )?;
    let secret: [u8; 32] = parse_hex_bytes(
        &params_get_str(&params, "secret", 2)
            .ok_or_else(|| BridgeError::InvalidParams("secret required".into()))?,
    )?;
    let signing_key_hex = params_get_str(&params, "signing_key", 3)
        .ok_or_else(|| BridgeError::InvalidParams("signing_key required".into()))?;

    let key_bytes: [u8; 32] = parse_hex_bytes(&signing_key_hex)?;
    let signer = EthSigningKey::from_bytes(key_bytes);
    let from_address = signer.address();

    let calldata = htlc::encode_claim(&swap_id, &secret);

    let chain_id = rpc
        .chain_id()
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_chainId: {}", e)))?;
    let nonce = rpc
        .get_nonce(&format!("0x{}", hex::encode(from_address)))
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_getTransactionCount: {}", e)))?;

    let gas_limit = params
        .get("gas_limit")
        .and_then(|v| v.as_u64())
        .unwrap_or(100_000);
    let max_priority = params
        .get("max_priority_fee_gwei")
        .and_then(|v| v.as_u64())
        .unwrap_or(2)
        * 1_000_000_000;
    let max_fee = params
        .get("max_fee_gwei")
        .and_then(|v| v.as_u64())
        .unwrap_or(50)
        * 1_000_000_000;

    let tx = Eip1559Tx {
        chain_id,
        nonce,
        max_priority_fee_per_gas: max_priority,
        max_fee_per_gas: max_fee,
        gas_limit,
        to: htlc_addr,
        value: [0u8; 32],
        data: calldata,
    };

    let signed = signer.sign_tx(&tx);
    let raw_hex = format!("0x{}", hex::encode(&signed.raw));

    let broadcast_hash = rpc
        .send_raw_tx(&raw_hex)
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_sendRawTransaction: {}", e)))?;

    log::info!("ETH HTLC claim broadcast: tx_hash={}", broadcast_hash);

    Ok(serde_json::json!({
        "success": true,
        "tx_hash": broadcast_hash,
        "from": format!("0x{}", hex::encode(from_address)),
        "secret_revealed": format!("0x{}", hex::encode(secret)),
    }))
}

/// Refund locked funds after timelock expiry.
///
/// Params: {
///   "htlc_address": "0x...",
///   "swap_id": "0x...",
///   "signing_key": "0x...",
///   "gas_limit": 100000,
///   "max_fee_gwei": 50,
///   "max_priority_fee_gwei": 2
/// }
async fn handle_eth_refund_htlc(
    eth_client: &Option<EthRpcClient>,
    params: Value,
) -> Result<Value, BridgeError> {
    let rpc = require_eth_client(eth_client)?;

    let htlc_addr: [u8; 20] = parse_hex_bytes(
        &params_get_str(&params, "htlc_address", 0)
            .ok_or_else(|| BridgeError::InvalidParams("htlc_address required".into()))?,
    )?;
    let swap_id: [u8; 32] = parse_hex_bytes(
        &params_get_str(&params, "swap_id", 1)
            .ok_or_else(|| BridgeError::InvalidParams("swap_id required".into()))?,
    )?;
    let signing_key_hex = params_get_str(&params, "signing_key", 2)
        .ok_or_else(|| BridgeError::InvalidParams("signing_key required".into()))?;

    let key_bytes: [u8; 32] = parse_hex_bytes(&signing_key_hex)?;
    let signer = EthSigningKey::from_bytes(key_bytes);
    let from_address = signer.address();

    let calldata = htlc::encode_refund(&swap_id);

    let chain_id = rpc
        .chain_id()
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_chainId: {}", e)))?;
    let nonce = rpc
        .get_nonce(&format!("0x{}", hex::encode(from_address)))
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_getTransactionCount: {}", e)))?;

    let gas_limit = params
        .get("gas_limit")
        .and_then(|v| v.as_u64())
        .unwrap_or(100_000);
    let max_priority = params
        .get("max_priority_fee_gwei")
        .and_then(|v| v.as_u64())
        .unwrap_or(2)
        * 1_000_000_000;
    let max_fee = params
        .get("max_fee_gwei")
        .and_then(|v| v.as_u64())
        .unwrap_or(50)
        * 1_000_000_000;

    let tx = Eip1559Tx {
        chain_id,
        nonce,
        max_priority_fee_per_gas: max_priority,
        max_fee_per_gas: max_fee,
        gas_limit,
        to: htlc_addr,
        value: [0u8; 32],
        data: calldata,
    };

    let signed = signer.sign_tx(&tx);
    let raw_hex = format!("0x{}", hex::encode(&signed.raw));

    let broadcast_hash = rpc
        .send_raw_tx(&raw_hex)
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_sendRawTransaction: {}", e)))?;

    log::info!("ETH HTLC refund broadcast: tx_hash={}", broadcast_hash);

    Ok(serde_json::json!({
        "success": true,
        "tx_hash": broadcast_hash,
        "from": format!("0x{}", hex::encode(from_address)),
    }))
}

/// Query HTLC swap state + confirmation depth.
///
/// Params: {
///   "htlc_address": "0x...",
///   "swap_id": "0x...",
///   "lock_tx_hash": "0x..." | null  // optional: track confirmation depth of lock tx
/// }
///
/// Returns the on-chain HTLC state and, if lock_tx_hash is provided,
/// the confirmation depth of the lock transaction.
async fn handle_eth_get_swap_status(
    eth_client: &Option<EthRpcClient>,
    params: Value,
) -> Result<Value, BridgeError> {
    let rpc = require_eth_client(eth_client)?;

    let htlc_addr_str = params_get_str(&params, "htlc_address", 0)
        .ok_or_else(|| BridgeError::InvalidParams("htlc_address required".into()))?;
    let swap_id: [u8; 32] = parse_hex_bytes(
        &params_get_str(&params, "swap_id", 1)
            .ok_or_else(|| BridgeError::InvalidParams("swap_id required".into()))?,
    )?;

    // Call getSwap(swapId) on the contract
    let calldata = htlc::encode_get_swap(&swap_id);
    let call_result = rpc
        .eth_call(
            &htlc_addr_str,
            &format!("0x{}", hex::encode(&calldata)),
            "latest",
        )
        .await
        .map_err(|e| BridgeError::InvalidParams(format!("eth_call: {}", e)))?;

    let result_bytes = hex::decode(call_result.strip_prefix("0x").unwrap_or(&call_result))
        .map_err(|e| BridgeError::InvalidParams(format!("result hex: {}", e)))?;

    let swap_info = htlc::decode_get_swap(&result_bytes);

    let (state, state_name, sender, recipient, token, amount_hex, secret_hash, timelock) =
        match &swap_info {
            Some(info) => {
                let state_name = match info.state {
                    htlc::HtlcState::Empty => "empty",
                    htlc::HtlcState::Locked => "locked",
                    htlc::HtlcState::Claimed => "claimed",
                    htlc::HtlcState::Refunded => "refunded",
                };
                (
                    info.state as u8,
                    state_name,
                    format!("0x{}", hex::encode(info.sender)),
                    format!("0x{}", hex::encode(info.recipient)),
                    format!("0x{}", hex::encode(info.token_address)),
                    format!("0x{}", hex::encode(info.amount)),
                    format!("0x{}", hex::encode(info.secret_hash)),
                    info.timelock,
                )
            }
            None => (
                0,
                "empty",
                String::new(),
                String::new(),
                String::new(),
                String::new(),
                String::new(),
                0,
            ),
        };

    // Resolve lock tx hash: use provided value, or scan chain if locked
    let mut lock_tx_hash = params_get_opt_str(&params, "lock_tx_hash", 2);
    let lookback_blocks: u64 = params
        .get("lookback_blocks")
        .and_then(|v| v.as_u64())
        .unwrap_or(10000);

    // If state is Locked and no tx hash provided, scan for the Locked event
    if state == 1 && lock_tx_hash.is_none() {
        let current_block = rpc.block_number().await.unwrap_or(0);
        if current_block > 0 {
            let from_block = current_block.saturating_sub(lookback_blocks);
            let config = watcher::WatcherConfig {
                contract_address: htlc_addr_str.clone(),
                min_confirmations: 0,
                lookback_blocks,
            };
            if let Ok(events) =
                watcher::scan_htlc_events(rpc, &config, from_block, current_block).await
            {
                let swap_id_hex = format!("0x{}", hex::encode(swap_id));
                for ev in &events {
                    if ev.event_type == watcher::HtlcEventType::Locked && ev.swap_id == swap_id_hex
                    {
                        lock_tx_hash = Some(ev.tx_hash.clone());
                        break;
                    }
                }
            }
        }
    }

    let conf_depth = if let Some(ref tx_hash) = lock_tx_hash {
        watcher::get_confirmation_depth(rpc, tx_hash)
            .await
            .map_err(|e| BridgeError::InvalidParams(format!("confirmation check: {}", e)))?
    } else {
        None
    };

    Ok(serde_json::json!({
        "success": true,
        "swap_id": format!("0x{}", hex::encode(swap_id)),
        "state": state,
        "state_name": state_name,
        "sender": sender,
        "recipient": recipient,
        "token_address": token,
        "amount": amount_hex,
        "secret_hash": secret_hash,
        "timelock": timelock,
        "confirmation_depth": conf_depth,
        "lock_tx_hash": lock_tx_hash,
    }))
}

// ============================================================================
// ETH Oracle Attestation Verification
// ============================================================================

fn handle_eth_verify_attestation(
    params: serde_json::Value,
) -> Result<serde_json::Value, BridgeError> {
    let oracle_pubkey = params_get_str(&params, "oracle_pubkey", 0)
        .ok_or_else(|| BridgeError::InvalidParams("missing oracle_pubkey".to_string()))?;
    let attestation_json = params
        .get("attestation")
        .ok_or_else(|| BridgeError::InvalidParams("missing attestation".to_string()))?;

    let attestation: watcher::OracleAttestation = serde_json::from_value(attestation_json.clone())
        .map_err(|e| BridgeError::InvalidParams(format!("invalid attestation: {}", e)))?;

    match watcher::verify_attestation(&oracle_pubkey, &attestation) {
        Ok(true) => Ok(serde_json::json!({
            "valid": true,
            "swap_id": attestation.swap_id,
            "confirmation_depth": attestation.confirmation_depth,
        })),
        Ok(false) => Ok(serde_json::json!({
            "valid": false,
            "error": "Signature verification returned false",
        })),
        Err(e) => Ok(serde_json::json!({
            "valid": false,
            "error": e,
        })),
    }
}

// ============================================================================
// ETH Signer Reference Resolution
// ============================================================================

fn handle_eth_resolve_signer(
    eth_derivation_seed: &Option<[u8; 32]>,
    params: serde_json::Value,
) -> Result<serde_json::Value, BridgeError> {
    let signer_ref = params_get_str(&params, "signer_ref", 0)
        .ok_or_else(|| BridgeError::InvalidParams("missing signer_ref".to_string()))?;

    if signer_ref.starts_with("derived:") {
        // Derive ETH key from the seed provided at eth_init time.
        // V1: HMAC-SHA512 the seed with "tensorcash-eth-{suffix}" to produce
        // a deterministic 32-byte private key.  This is NOT BIP-32 — it's a
        // simple one-level derivation that's sufficient for v1.  Full BIP-32
        // can be added later if multi-account support is needed.
        let seed = eth_derivation_seed.as_ref().ok_or_else(|| {
            BridgeError::InvalidParams(
                "derived:auto requires a derivation_seed in eth_init. \
                 Pass derivation_seed=<32-byte-hex> when calling eth_init."
                    .to_string(),
            )
        })?;

        let suffix = signer_ref.strip_prefix("derived:").unwrap(); // "auto", "0", "1", etc.
        let tag = format!("tensorcash-eth-{}", suffix);

        // HMAC-SHA256(seed, tag) → 32-byte private key
        use hmac::{Hmac, Mac};
        use sha2::Sha256;
        type HmacSha256 = Hmac<Sha256>;
        let mut mac = HmacSha256::new_from_slice(seed)
            .map_err(|e| BridgeError::InvalidParams(format!("HMAC init: {}", e)))?;
        mac.update(tag.as_bytes());
        let result = mac.finalize().into_bytes();

        let mut key_bytes = [0u8; 32];
        key_bytes.copy_from_slice(&result[..32]);

        let key = EthSigningKey::from_bytes(key_bytes);
        let key_hex = hex::encode(key_bytes);
        let addr_hex = hex::encode(key.address());

        log::info!("Resolved derived:{} → 0x{}", suffix, addr_hex);

        return Ok(serde_json::json!({
            "signing_key": key_hex,
            "address": format!("0x{}", addr_hex),
        }));
    }

    if signer_ref.starts_with("imported:") {
        let key_id = signer_ref.strip_prefix("imported:").unwrap();
        return Err(BridgeError::InvalidParams(format!(
            "imported signer_ref '{}' not found in keystore. \
                     Use a raw hex signing key or derived:auto.",
            key_id
        )));
    }

    // Raw hex key — validate length and pass through
    let clean = signer_ref.strip_prefix("0x").unwrap_or(&signer_ref);
    if clean.len() != 64 || hex::decode(clean).is_err() {
        return Err(BridgeError::InvalidParams(format!(
            "signer_ref must be 32-byte hex, 'derived:auto', or 'imported:<id>'. Got: {}",
            signer_ref
        )));
    }

    let key = EthSigningKey::from_bytes({
        let mut buf = [0u8; 32];
        buf.copy_from_slice(&hex::decode(clean).unwrap());
        buf
    });

    Ok(serde_json::json!({
        "signing_key": clean,
        "address": format!("0x{}", hex::encode(key.address())),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_bad_configuration_error() {
        let error = BridgeError::BadConfiguration("test config error".to_string());
        assert_eq!(error.to_string(), "Bad configuration: test config error");
    }

    #[test]
    fn test_transport_down_error() {
        let error = BridgeError::TransportDown("WebSocket disconnected".to_string());
        assert_eq!(error.to_string(), "Transport down: WebSocket disconnected");
    }

    #[test]
    fn test_crypto_init_failed_error() {
        let error = BridgeError::CryptoInitFailed("Failed to initialize SPAKE2".to_string());
        assert_eq!(
            error.to_string(),
            "Crypto initialization failed: Failed to initialize SPAKE2"
        );
    }

    #[test]
    fn test_invalid_command_error() {
        let error = BridgeError::InvalidCommand("unknown command".to_string());
        assert_eq!(error.to_string(), "Invalid command: unknown command");
    }

    #[test]
    fn test_session_not_found_error() {
        let error = BridgeError::SessionNotFound("session_123".to_string());
        assert_eq!(error.to_string(), "Session not found: session_123");
    }

    #[test]
    fn test_version_response_serialization() {
        let response = VersionResponse {
            api_version: 1,
            git_commit: "abc123".to_string(),
            build_flags: vec!["noise".to_string()],
            bridge_version: "0.1.0".to_string(),
        };

        let json = serde_json::to_value(&response).unwrap();
        assert_eq!(json.get("api_version").unwrap(), 1);
        assert_eq!(json.get("git_commit").unwrap(), "abc123");
    }

    #[test]
    fn test_ping_response_serialization() {
        let response = PingResponse {
            bridge_alive: true,
            version: "0.1.0".to_string(),
            transports: vec!["ws".to_string()],
            uptime_sec: 42,
            capabilities: vec!["resume".to_string()],
        };

        let json = serde_json::to_value(&response).unwrap();
        assert_eq!(json.get("bridge_alive").unwrap(), true);
        assert_eq!(json.get("uptime_sec").unwrap(), 42);
    }

    #[test]
    fn test_request_deserialization() {
        let json = r#"{"command": "ping", "params": {}}"#;
        let request: Request = serde_json::from_str(json).unwrap();
        assert_eq!(request.command, "ping");
    }

    #[test]
    fn test_request_deserialization_with_params() {
        let json = r#"{"command": "init", "params": {"ttl": 1800}}"#;
        let request: Request = serde_json::from_str(json).unwrap();
        assert_eq!(request.command, "init");
        assert_eq!(request.params.get("ttl").unwrap(), 1800);
    }

    #[test]
    fn test_response_serialization_success() {
        let response = Response {
            error: None,
            data: serde_json::json!({"result": "ok"}),
        };

        let json = serde_json::to_string(&response).unwrap();
        assert!(json.contains("ok"));
        assert!(!json.contains("error"));
    }

    #[test]
    fn test_response_serialization_error() {
        let response = Response {
            error: Some("test error".to_string()),
            data: serde_json::json!({}),
        };

        let json = serde_json::to_string(&response).unwrap();
        assert!(json.contains("test error"));
    }

    #[test]
    fn test_bridge_error_variants() {
        // Test that all error variants can be constructed and used
        let errors = vec![
            BridgeError::BadConfiguration("config".to_string()),
            BridgeError::TransportDown("ws".to_string()),
            BridgeError::CryptoInitFailed("crypto".to_string()),
            BridgeError::InvalidCommand("cmd".to_string()),
            BridgeError::SessionNotFound("sess".to_string()),
        ];

        for error in errors {
            // Each error should have a non-empty string representation
            assert!(!error.to_string().is_empty());
        }
    }
}
