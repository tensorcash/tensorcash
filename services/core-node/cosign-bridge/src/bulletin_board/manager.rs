// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Bulletin board manager (orchestration layer)

use crate::bulletin_board::discussion::{
    build_scope_key, validate_scope, DiscussionPost, DiscussionScope,
};
use crate::bulletin_board::governance::{
    GovernanceAccessRequest, GovernanceBallot, GovernanceBallotDM, GovernanceBallotReceipt,
    GovernanceDMEnvelope, GovernanceDMType, GovernanceProposal, GovernanceProposalResponse,
};
use crate::bulletin_board::nostr::NostrClient;
use crate::bulletin_board::types::{
    Offer, OfferFilters, OfferState, OfferSummary, RequestDirection, RequestStatus, TradeRequest,
    TradeRequestSummary,
};
use crate::stdio::BridgeError;
use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::RwLock;

/// Bulletin board manager (orchestrates offers and trade requests)
pub struct BulletinBoardManager {
    /// Nostr client for publishing/querying
    pub nostr_client: NostrClient,

    /// Bitcoin network this manager operates on (main, signet, testnet3, regtest)
    /// All offers will be tagged with this network and queries will filter by it
    network: String,

    /// In-memory cache of offers (30min TTL)
    offer_cache: Arc<RwLock<HashMap<String, Offer>>>,

    /// In-memory cache of trade requests (1h TTL)
    request_cache: Arc<RwLock<HashMap<String, TradeRequest>>>,

    /// In-memory cache of governance proposals (1h TTL)
    governance_cache: Arc<RwLock<HashMap<String, GovernanceProposal>>>,

    /// In-memory cache of governance ballots (proposal_id -> Vec<GovernanceBallot>)
    ballot_cache: Arc<RwLock<HashMap<String, Vec<GovernanceBallot>>>>,

    /// In-memory cache of discussion posts keyed by Nostr event ID.
    discussion_cache: Arc<RwLock<HashMap<String, DiscussionPost>>>,

    /// Last time offers were refreshed from Nostr
    last_offer_refresh: Arc<RwLock<Instant>>,

    /// Last time governance proposals were refreshed from Nostr
    last_governance_refresh: Arc<RwLock<Instant>>,

    /// Last refresh timestamp per discussion scope key.
    discussion_scope_refresh: Arc<RwLock<HashMap<String, Instant>>>,

    /// Rate limiting: track last proposal timestamp per asset
    asset_last_proposal: Arc<RwLock<HashMap<String, u64>>>,

    /// Replay protection: track processed governance DM message IDs
    /// Key: SHA256(dm_type + proposal_id + sequence + timestamp)
    governance_dm_seen: Arc<RwLock<HashSet<String>>>,

    /// Private governance payload cache (persistent storage)
    /// Stores full ICU text, template PSBT, witness bundle for private proposals.
    /// These fields are never published to Nostr relays; only sent via encrypted DM
    /// after verifying asset ownership.
    /// Survives bridge restarts via sled DB.
    /// Key: proposal_id
    private_payloads_db: sled::Db,

    /// Received proposal responses cache (holder-side, persistent storage)
    /// Stores private proposal data received via encrypted DM from issuers.
    /// Merged with public proposals during list_governance to maintain GRANTED state.
    /// Survives bridge restarts via sled DB.
    /// Key: proposal_id
    received_proposals_db: sled::Db,

    /// Governance DM replay protection cache (persistent storage)
    /// Stores processed envelope hashes and conversation state to prevent replays.
    /// Survives bridge restarts via sled DB.
    /// Key: `{proposal_id}:{sequence}` → envelope_hash
    ///
    /// **Session Tracking**: This DB implicitly tracks governance conversation sessions.
    /// Each proposal_id defines a session, and stored sequences indicate conversation progress:
    /// - Sequence 1 (AccessRequest): Holder initiated request
    /// - Sequence 2 (ProposalResponse): Issuer sent response (GRANTED state)
    /// - Sequence 3 (Ballot): Holder submitted vote
    /// - Sequence 4 (BallotReceipt): Issuer acknowledged vote
    ///
    /// Query session state via `get_session_status(proposal_id)` to find max sequence.
    governance_replay_db: sled::Db,

    /// Persisted discussion post cache.
    /// Key: Nostr event ID
    discussion_posts_db: sled::Db,
}

pub struct DiscussionListResult {
    pub posts: Vec<DiscussionPost>,
    pub stale: bool,
    pub refresh_error: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiscussionScopeSummary {
    pub scope_type: String,
    pub scope_id: String,
    pub latest_created_at: u64,
    pub post_count: u64,
    pub latest_post_id: String,
    pub latest_content_preview: String,
    pub model_identifier: Option<String>,
}

/// Private governance proposal data (never published to Nostr)
///
/// For privacy-sensitive governance proposals, sensitive fields are stripped
/// before broadcasting to public Nostr relays. The full data is cached here
/// and only transmitted to verified asset holders via encrypted DMs.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct PrivateProposalData {
    pub proposal_id: String,
    pub icu_text: Option<String>,
    pub template_psbt: Option<String>,
    pub witness_bundle: Option<String>,
    pub canonical_icu_hash: Option<String>,
    pub template_psbt_hash: Option<String>,
    pub witness_bundle_hash: Option<String>,
}

impl BulletinBoardManager {
    /// Create new bulletin board manager
    ///
    /// # Arguments
    ///
    /// * `relays` - List of Nostr relay URLs
    /// * `nostr_key_path` - Optional path to Nostr key file
    /// * `network` - Bitcoin network (main, signet, testnet3, regtest) for offer compartmentalization
    pub async fn new(
        relays: Vec<String>,
        nostr_key_path: Option<String>,
        network: String,
    ) -> Result<Self, BridgeError> {
        // Initialize persistent storage for private payloads
        // Derive unique DB path from nostr_key_path to avoid conflicts between multiple instances
        let db_path = if let Some(ref key_path) = nostr_key_path {
            // Extract instance identifier from key path (e.g., "/tmp/nostr_keys_default__root_tensorcash-gui1_regtest")
            let key_file_name = std::path::Path::new(key_path)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("default");

            format!("/tmp/cosign_bridge_private_payloads_{}", key_file_name)
        } else {
            // Fallback if no key path provided
            let home_dir = std::env::var("HOME")
                .or_else(|_| std::env::var("USERPROFILE"))
                .unwrap_or_else(|_| ".".to_string());
            format!("{}/.tensorcash/cosign_bridge/private_payloads", home_dir)
        };

        // Create parent directory if it doesn't exist
        if let Some(parent) = std::path::Path::new(&db_path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                BridgeError::BadConfiguration(format!(
                    "Failed to create directory {}: {}",
                    parent.display(),
                    e
                ))
            })?;
        }

        let private_payloads_db = sled::open(&db_path).map_err(|e| {
            BridgeError::BadConfiguration(format!(
                "Failed to open private payloads DB at {}: {}",
                db_path, e
            ))
        })?;

        log::info!("Initialized private payloads DB at: {}", db_path);

        // Initialize persistent storage for received proposal responses (holder-side)
        let received_db_path = db_path.replace("private_payloads", "received_proposals");

        if let Some(parent) = std::path::Path::new(&received_db_path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                BridgeError::BadConfiguration(format!(
                    "Failed to create directory {}: {}",
                    parent.display(),
                    e
                ))
            })?;
        }

        let received_proposals_db = sled::open(&received_db_path).map_err(|e| {
            BridgeError::BadConfiguration(format!(
                "Failed to open received proposals DB at {}: {}",
                received_db_path, e
            ))
        })?;

        log::info!("Initialized received proposals DB at: {}", received_db_path);

        // Initialize persistent replay protection cache for governance DMs
        let replay_db_path = db_path.replace("private_payloads", "governance_replay");

        if let Some(parent) = std::path::Path::new(&replay_db_path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                BridgeError::BadConfiguration(format!(
                    "Failed to create directory {}: {}",
                    parent.display(),
                    e
                ))
            })?;
        }

        let governance_replay_db = sled::open(&replay_db_path).map_err(|e| {
            BridgeError::BadConfiguration(format!(
                "Failed to open governance replay DB at {}: {}",
                replay_db_path, e
            ))
        })?;

        log::info!("Initialized governance replay DB at: {}", replay_db_path);

        let discussion_db_path = db_path.replace("private_payloads", "discussion_posts");

        if let Some(parent) = std::path::Path::new(&discussion_db_path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                BridgeError::BadConfiguration(format!(
                    "Failed to create directory {}: {}",
                    parent.display(),
                    e
                ))
            })?;
        }

        let discussion_posts_db = sled::open(&discussion_db_path).map_err(|e| {
            BridgeError::BadConfiguration(format!(
                "Failed to open discussion posts DB at {}: {}",
                discussion_db_path, e
            ))
        })?;

        log::info!("Initialized discussion posts DB at: {}", discussion_db_path);

        let mut persisted_discussion_posts = HashMap::new();
        for entry in discussion_posts_db.iter() {
            let (_, value) = entry.map_err(|e| {
                BridgeError::BadConfiguration(format!(
                    "Failed to iterate discussion posts DB: {}",
                    e
                ))
            })?;

            match serde_json::from_slice::<DiscussionPost>(&value) {
                Ok(post) => {
                    persisted_discussion_posts.insert(post.post_id.clone(), post);
                }
                Err(e) => {
                    log::warn!(
                        "Skipping malformed discussion post in persisted cache: {}",
                        e
                    );
                }
            }
        }

        log::info!(
            "Loaded {} persisted discussion posts into cache",
            persisted_discussion_posts.len()
        );

        let nostr_client = NostrClient::new(relays, nostr_key_path).await?;

        log::info!("Bulletin board initialized for network: {}", network);

        Ok(Self {
            nostr_client,
            network,
            offer_cache: Arc::new(RwLock::new(HashMap::new())),
            request_cache: Arc::new(RwLock::new(HashMap::new())),
            governance_cache: Arc::new(RwLock::new(HashMap::new())),
            ballot_cache: Arc::new(RwLock::new(HashMap::new())),
            discussion_cache: Arc::new(RwLock::new(persisted_discussion_posts)),
            last_offer_refresh: Arc::new(RwLock::new(Instant::now())),
            last_governance_refresh: Arc::new(RwLock::new(Instant::now())),
            discussion_scope_refresh: Arc::new(RwLock::new(HashMap::new())),
            asset_last_proposal: Arc::new(RwLock::new(HashMap::new())),
            governance_dm_seen: Arc::new(RwLock::new(HashSet::new())),
            private_payloads_db,
            received_proposals_db,
            governance_replay_db,
            discussion_posts_db,
        })
    }

    /// Get connected relay URLs
    pub fn get_relay_urls(&self) -> Vec<String> {
        self.nostr_client.get_relays()
    }

    /// Get Nostr public key
    pub fn get_pubkey(&self) -> String {
        self.nostr_client.get_public_key()
    }

    /// Get Bitcoin network this manager is configured for
    pub fn get_network(&self) -> &str {
        &self.network
    }

    /// Post an offer to the bulletin board
    ///
    /// # Arguments
    ///
    /// * `offer` - Offer to publish
    ///
    /// # Returns
    ///
    /// Offer ID
    pub async fn post_offer(&mut self, mut offer: Offer) -> Result<String, BridgeError> {
        // Publish to Nostr
        let event_id = self.nostr_client.publish_offer(&offer).await?;
        offer.nostr_event_id = Some(event_id);

        // Cache locally
        let offer_id = offer.id.clone();
        self.offer_cache
            .write()
            .await
            .insert(offer_id.clone(), offer);

        Ok(offer_id)
    }

    /// List offers from bulletin board (with optional filters)
    ///
    /// # Arguments
    ///
    /// * `filters` - Optional filters to apply
    ///
    /// # Returns
    ///
    /// List of offers matching filters
    pub async fn list_offers(&mut self, filters: OfferFilters) -> Result<Vec<Offer>, BridgeError> {
        // Refresh cache if stale (>5min)
        self.refresh_offers_if_needed().await?;

        // Get offers from cache
        let cache = self.offer_cache.read().await;
        let mut offers: Vec<Offer> = cache.values().cloned().collect();

        // SECURITY: Only return offers in Posted state (not Accepted/Cancelled)
        // This prevents leaking session_id and invite_link to public listeners
        offers.retain(|offer| matches!(offer.state, OfferState::Posted));

        // IMPORTANT: Always filter by network to compartmentalize chains
        // This is a safety measure even though nostr.rs should have already filtered
        let network = self.network.clone();
        offers.retain(|offer| offer.network == network);

        // Apply client-side filters (already done in nostr.rs, but double-check)
        offers.retain(|offer| {
            if let Some(ref offer_type) = filters.offer_type {
                let matches = match offer.offer_type {
                    crate::bulletin_board::types::OfferType::Buy => offer_type == "buy",
                    crate::bulletin_board::types::OfferType::Sell => offer_type == "sell",
                    crate::bulletin_board::types::OfferType::Swap => offer_type == "swap",
                    crate::bulletin_board::types::OfferType::RepoContract => {
                        offer_type == "repo_contract"
                    }
                    crate::bulletin_board::types::OfferType::ForwardContract => {
                        offer_type == "forward_contract"
                    }
                    crate::bulletin_board::types::OfferType::SpotContract => {
                        offer_type == "spot_contract"
                    }
                    crate::bulletin_board::types::OfferType::DifficultyContract => {
                        offer_type == "difficulty_contract"
                    }
                };
                if !matches {
                    return false;
                }
            }

            if let Some(min_rep) = filters.min_reputation {
                if offer.min_reputation_score < min_rep {
                    return false;
                }
            }

            // Contract-specific filters
            if let Some(ref contract_type_filter) = filters.contract_type {
                match &offer.contract_type {
                    Some(contract_type) => {
                        let matches = match contract_type {
                            crate::bulletin_board::types::ContractType::Repo => {
                                contract_type_filter == "repo"
                            }
                            crate::bulletin_board::types::ContractType::Forward => {
                                contract_type_filter == "forward"
                            }
                            crate::bulletin_board::types::ContractType::Spot => {
                                contract_type_filter == "spot"
                            }
                            crate::bulletin_board::types::ContractType::Difficulty => {
                                contract_type_filter == "difficulty"
                            }
                        };
                        if !matches {
                            return false;
                        }
                    }
                    None => return false, // Filter requests contract but offer isn't one
                }
            }

            if let Some(ref role_filter) = filters.maker_role {
                match &offer.maker_role {
                    Some(role) => {
                        if role != role_filter {
                            return false;
                        }
                    }
                    None => return false,
                }
            }

            if let Some(min_apr) = filters.min_apr {
                match offer.apr {
                    Some(apr) if apr < min_apr => return false,
                    None => return false,
                    _ => {}
                }
            }

            if let Some(max_apr) = filters.max_apr {
                match offer.apr {
                    Some(apr) if apr > max_apr => return false,
                    None => return false,
                    _ => {}
                }
            }

            if let Some(min_tenor) = filters.min_tenor_days {
                match offer.tenor_days {
                    Some(tenor) if tenor < min_tenor => return false,
                    None => return false,
                    _ => {}
                }
            }

            if let Some(max_tenor) = filters.max_tenor_days {
                match offer.tenor_days {
                    Some(tenor) if tenor > max_tenor => return false,
                    None => return false,
                    _ => {}
                }
            }

            !offer.is_expired()
        });

        // Sort by creation time (newest first)
        offers.sort_by(|a, b| b.created_at.cmp(&a.created_at));

        Ok(offers)
    }

    /// Get a specific offer by ID
    ///
    /// # Arguments
    ///
    /// * `offer_id` - Offer ID to fetch
    ///
    /// # Returns
    ///
    /// Offer if found (checks cache first, falls back to Nostr query)
    pub async fn get_offer(&mut self, offer_id: &str) -> Result<Offer, BridgeError> {
        // First check cache
        {
            let cache = self.offer_cache.read().await;
            if let Some(offer) = cache.get(offer_id) {
                return Ok(offer.clone());
            }
        }

        // Cache miss - query Nostr for all offers and find the one we want
        log::debug!("Offer {} not in cache, querying Nostr", offer_id);
        let offers = self
            .nostr_client
            .query_offers(OfferFilters::default())
            .await?;

        // Update cache with all fetched offers
        let mut cache = self.offer_cache.write().await;
        for offer in offers {
            if offer.id == offer_id {
                // Found it - cache and return
                cache.insert(offer.id.clone(), offer.clone());
                return Ok(offer);
            } else {
                // Cache other offers too while we're at it
                cache.insert(offer.id.clone(), offer);
            }
        }

        Err(BridgeError::SessionNotFound(format!(
            "Offer not found: {}",
            offer_id
        )))
    }

    /// Delete an offer (maker only)
    ///
    /// # Arguments
    ///
    /// * `offer_id` - Offer ID to delete
    ///
    /// # Note
    ///
    /// This should verify that the caller is the maker (TODO: add authentication)
    pub async fn delete_offer(&mut self, offer_id: &str) -> Result<(), BridgeError> {
        // Get offer from cache
        let offer = self.get_offer(offer_id).await?;

        // Delete from Nostr (if event ID is known)
        if let Some(event_id) = offer.nostr_event_id {
            self.nostr_client.delete_offer(&event_id).await?;
        }

        // Remove from cache
        self.offer_cache.write().await.remove(offer_id);

        Ok(())
    }

    /// Request a trade (taker sends DM to maker)
    ///
    /// # Arguments
    ///
    /// * `offer_id` - Offer ID to request
    /// * `taker_pubkey` - Taker's Nostr public key
    /// * `message` - Optional message from taker
    ///
    /// # Returns
    ///
    /// Request ID
    pub async fn request_trade(
        &mut self,
        offer_id: &str,
        taker_pubkey: &str,
        message: Option<String>,
        proof_of_funds: Option<Vec<crate::bulletin_board::governance::OwnershipProof>>,
    ) -> Result<String, BridgeError> {
        // Get offer
        let offer = self.get_offer(offer_id).await?;

        // Verify offer can accept requests
        if !offer.can_accept_requests() {
            return Err(BridgeError::InvalidCommand(format!(
                "Offer {} cannot accept requests (state: {:?})",
                offer_id, offer.state
            )));
        }

        // Create trade request
        let mut request = TradeRequest::new(
            offer_id.to_string(),
            taker_pubkey.to_string(),
            offer.maker_pubkey.clone(),
            message.clone(),
        );

        // Attach proof of funds if provided
        request.proof_of_funds = proof_of_funds.clone();

        // Build DM content with optional proof_of_funds
        let mut dm_content = serde_json::json!({
            "type": "trade_request",
            "request_id": request.id,
            "offer_id": offer_id,
            "taker_pubkey": taker_pubkey,
            "message": message,
        });

        // Include proof_of_funds in DM if provided
        if let Some(proofs) = &proof_of_funds {
            dm_content["proof_of_funds"] =
                serde_json::to_value(proofs).unwrap_or(serde_json::Value::Null);
        }

        self.nostr_client
            .send_dm(&offer.maker_pubkey, &dm_content.to_string())
            .await?;

        // Cache request
        let request_id = request.id.clone();
        self.request_cache
            .write()
            .await
            .insert(request_id.clone(), request);

        Ok(request_id)
    }

    /// List trade requests relevant to the current user
    ///
    /// Returns both incoming (you are the maker) and outgoing (you are the taker)
    /// requests, enriched with offer metadata when available.
    pub async fn list_trade_requests(
        &mut self,
        user_pubkey: &str,
    ) -> Result<Vec<TradeRequestSummary>, BridgeError> {
        // Fetch DMs (incoming + outgoing) and process oldest-first
        let mut dms = self.nostr_client.fetch_dms(None).await?;
        dms.sort_by_key(|dm| dm.timestamp);

        {
            let mut cache = self.request_cache.write().await;

            for dm in &dms {
                let Ok(dm_json) = serde_json::from_str::<serde_json::Value>(&dm.content) else {
                    continue;
                };

                match dm_json.get("type").and_then(|v| v.as_str()) {
                    Some("trade_request") => {
                        let Some(request_id) = dm_json.get("request_id").and_then(|v| v.as_str())
                        else {
                            continue;
                        };
                        let Some(offer_id_val) = dm_json.get("offer_id").and_then(|v| v.as_str())
                        else {
                            continue;
                        };
                        let Some(taker_key) = dm_json.get("taker_pubkey").and_then(|v| v.as_str())
                        else {
                            continue;
                        };

                        let request_id = request_id.to_string();
                        let offer_id = offer_id_val.to_string();
                        let taker_pubkey = taker_key.to_string();
                        let maker_pubkey = dm.to_pubkey.clone();
                        let message = dm_json
                            .get("message")
                            .and_then(|v| v.as_str())
                            .map(String::from);

                        // Parse optional proof_of_funds from DM
                        let proof_of_funds = dm_json.get("proof_of_funds").and_then(|v| {
                            serde_json::from_value::<
                                Vec<crate::bulletin_board::governance::OwnershipProof>,
                            >(v.clone())
                            .ok()
                        });

                        let entry =
                            cache
                                .entry(request_id.clone())
                                .or_insert_with(|| TradeRequest {
                                    id: request_id.clone(),
                                    offer_id: offer_id.clone(),
                                    taker_pubkey: taker_pubkey.clone(),
                                    maker_pubkey: maker_pubkey.clone(),
                                    timestamp: dm.timestamp,
                                    message: message.clone(),
                                    status: RequestStatus::Pending,
                                    invite_link: None,
                                    invite_expires_at: None,
                                    updated_at: dm.timestamp,
                                    proof_of_funds: proof_of_funds.clone(),
                                });

                        entry.offer_id = offer_id;
                        entry.taker_pubkey = taker_pubkey;
                        entry.maker_pubkey = maker_pubkey;
                        entry.message = message;
                        entry.proof_of_funds = proof_of_funds;
                        entry.timestamp = entry.timestamp.min(dm.timestamp);
                        entry.updated_at = entry.updated_at.max(dm.timestamp);
                    }
                    Some("trade_accepted") => {
                        let Some(request_id) = dm_json.get("request_id").and_then(|v| v.as_str())
                        else {
                            continue;
                        };
                        let invite_link = dm_json
                            .get("invite_link")
                            .and_then(|v| v.as_str())
                            .map(String::from);
                        let invite_expires_at = dm_json.get("expires_at").and_then(|v| v.as_u64());

                        if let Some(entry) = cache.get_mut(request_id) {
                            entry.status = RequestStatus::Accepted;
                            entry.invite_link = invite_link;
                            entry.invite_expires_at = invite_expires_at;
                            entry.updated_at = entry.updated_at.max(dm.timestamp);
                        } else {
                            log::warn!(
                                "Trade acceptance received for unknown request {}",
                                request_id
                            );
                        }
                    }
                    Some("trade_rejected") => {
                        let Some(request_id) = dm_json.get("request_id").and_then(|v| v.as_str())
                        else {
                            continue;
                        };

                        if let Some(entry) = cache.get_mut(request_id) {
                            entry.status = RequestStatus::Rejected;
                            entry.invite_link = None;
                            entry.invite_expires_at = None;
                            entry.updated_at = entry.updated_at.max(dm.timestamp);
                        } else {
                            log::warn!(
                                "Trade rejection received for unknown request {}",
                                request_id
                            );
                        }
                    }
                    Some("trade_cancelled") => {
                        let Some(request_id) = dm_json.get("request_id").and_then(|v| v.as_str())
                        else {
                            continue;
                        };

                        if let Some(entry) = cache.get_mut(request_id) {
                            entry.status = RequestStatus::Cancelled;
                            entry.invite_link = None;
                            entry.invite_expires_at = None;
                            entry.updated_at = entry.updated_at.max(dm.timestamp);
                        } else {
                            log::warn!(
                                "Trade cancellation received for unknown request {}",
                                request_id
                            );
                        }
                    }
                    _ => {}
                }
            }
        }

        let requests_snapshot: Vec<TradeRequest> = {
            let cache = self.request_cache.read().await;
            cache.values().cloned().collect()
        };

        let mut summaries = Vec::new();

        for request in requests_snapshot {
            let direction = if request.maker_pubkey == user_pubkey {
                RequestDirection::Incoming
            } else if request.taker_pubkey == user_pubkey {
                RequestDirection::Outgoing
            } else {
                continue;
            };

            let counterparty_pubkey = if direction == RequestDirection::Incoming {
                request.taker_pubkey.clone()
            } else {
                request.maker_pubkey.clone()
            };

            let offer = self.offer_snapshot(&request.offer_id).await;

            summaries.push(TradeRequestSummary {
                request,
                direction,
                counterparty_pubkey,
                offer,
            });
        }

        summaries.sort_by(|a, b| b.request.updated_at.cmp(&a.request.updated_at));

        Ok(summaries)
    }

    /// Accept a trade request (maker sends invite link to taker)
    ///
    /// # Arguments
    ///
    /// * `request_id` - Request ID to accept
    /// * `invite_link` - Ephemeral invite link for bilateral session
    ///
    /// # Note
    ///
    /// The invite_link is created by SessionManager.init() and passed here.
    /// This method sends it to the taker via DM.
    pub async fn accept_trade_request(
        &mut self,
        request_id: &str,
        invite_link: String,
    ) -> Result<(), BridgeError> {
        let request = self.load_request(request_id).await?;
        let invite_link_value = invite_link;

        // Update offer state
        if let Ok(mut offer) = self.get_offer(&request.offer_id).await {
            offer.state = OfferState::Accepted(request.taker_pubkey.clone());
            offer.invite_link = Some(invite_link_value.clone());
            self.offer_cache
                .write()
                .await
                .insert(offer.id.clone(), offer);
        }

        let now_ts = chrono::Utc::now().timestamp();
        let updated_at = if now_ts >= 0 { now_ts as u64 } else { 0 };
        let expires_ts = now_ts + 600;
        let invite_expires_at = if expires_ts >= 0 {
            Some(expires_ts as u64)
        } else {
            None
        };

        // Send DM to taker with invite link
        let dm_content = serde_json::json!({
            "type": "trade_accepted",
            "request_id": request_id,
            "invite_link": invite_link_value.clone(),
            "expires_at": expires_ts,  // 10min expiry
        });

        self.nostr_client
            .send_dm(&request.taker_pubkey, &dm_content.to_string())
            .await?;

        // Update request status in cache
        let mut cache = self.request_cache.write().await;
        if let Some(req) = cache.get_mut(request_id) {
            req.status = RequestStatus::Accepted;
            req.invite_link = Some(invite_link_value.clone());
            req.invite_expires_at = invite_expires_at;
            req.updated_at = updated_at;
        } else {
            let mut new_request = request.clone();
            new_request.status = RequestStatus::Accepted;
            new_request.invite_link = Some(invite_link_value.clone());
            new_request.invite_expires_at = invite_expires_at;
            new_request.updated_at = updated_at;
            cache.insert(request_id.to_string(), new_request);
        }

        Ok(())
    }

    /// Reject a trade request
    ///
    /// # Arguments
    ///
    /// * `request_id` - Request ID to reject
    /// * `reason` - Optional rejection reason
    pub async fn reject_trade_request(
        &mut self,
        request_id: &str,
        reason: Option<String>,
    ) -> Result<(), BridgeError> {
        let request = self.load_request(request_id).await?;

        // Send DM to taker
        let dm_content = serde_json::json!({
            "type": "trade_rejected",
            "request_id": request_id,
            "reason": reason,
        });

        self.nostr_client
            .send_dm(&request.taker_pubkey, &dm_content.to_string())
            .await?;

        let now_ts = chrono::Utc::now().timestamp();
        let updated_at = if now_ts >= 0 { now_ts as u64 } else { 0 };

        // Update request status in cache
        let mut cache = self.request_cache.write().await;
        if let Some(req) = cache.get_mut(request_id) {
            req.status = RequestStatus::Rejected;
            req.invite_link = None;
            req.invite_expires_at = None;
            req.updated_at = updated_at;
        } else {
            let mut new_request = request.clone();
            new_request.status = RequestStatus::Rejected;
            new_request.invite_link = None;
            new_request.invite_expires_at = None;
            new_request.updated_at = updated_at;
            cache.insert(request_id.to_string(), new_request);
        }

        Ok(())
    }

    /// Cancel a trade request (taker withdraws the request)
    pub async fn cancel_trade_request(
        &mut self,
        request_id: &str,
        reason: Option<String>,
    ) -> Result<(), BridgeError> {
        let request = self.load_request(request_id).await?;
        let caller = self.get_pubkey();

        if request.taker_pubkey != caller {
            return Err(BridgeError::InvalidCommand(
                "Only the originating taker can cancel this request".to_string(),
            ));
        }

        let cancelled_at = chrono::Utc::now().timestamp();
        let dm_content = serde_json::json!({
            "type": "trade_cancelled",
            "request_id": request_id,
            "reason": reason,
            "cancelled_at": cancelled_at,
        });

        self.nostr_client
            .send_dm(&request.maker_pubkey, &dm_content.to_string())
            .await?;

        let updated_at = if cancelled_at >= 0 {
            cancelled_at as u64
        } else {
            0
        };

        let mut cache = self.request_cache.write().await;
        if let Some(req) = cache.get_mut(request_id) {
            req.status = RequestStatus::Cancelled;
            req.invite_link = None;
            req.invite_expires_at = None;
            req.updated_at = updated_at;
        } else {
            let mut new_request = request.clone();
            new_request.status = RequestStatus::Cancelled;
            new_request.invite_link = None;
            new_request.invite_expires_at = None;
            new_request.updated_at = updated_at;
            cache.insert(request_id.to_string(), new_request);
        }

        Ok(())
    }

    /// Resolve a trade request from cache or Nostr DMs and ensure it is cached
    async fn load_request(&mut self, request_id: &str) -> Result<TradeRequest, BridgeError> {
        if let Some(request) = {
            let cache = self.request_cache.read().await;
            cache.get(request_id).cloned()
        } {
            return Ok(request);
        }

        log::debug!(
            "Request {} not in cache, fetching from Nostr DMs",
            request_id
        );

        let mut dms = self.nostr_client.fetch_dms(None).await?;
        dms.sort_by_key(|dm| dm.timestamp);

        for dm in dms {
            let Ok(dm_json) = serde_json::from_str::<serde_json::Value>(&dm.content) else {
                continue;
            };

            if dm_json.get("type").and_then(|v| v.as_str()) == Some("trade_request")
                && dm_json.get("request_id").and_then(|v| v.as_str()) == Some(request_id)
            {
                let offer_id = dm_json
                    .get("offer_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string();
                let taker_pubkey = dm_json
                    .get("taker_pubkey")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown")
                    .to_string();
                let maker_pubkey = dm.to_pubkey.clone();
                let message = dm_json
                    .get("message")
                    .and_then(|v| v.as_str())
                    .map(String::from);

                // Parse optional proof_of_funds from DM
                let proof_of_funds = dm_json.get("proof_of_funds").and_then(|v| {
                    serde_json::from_value::<
                        Vec<crate::bulletin_board::governance::OwnershipProof>,
                    >(v.clone())
                    .ok()
                });

                let request = TradeRequest {
                    id: request_id.to_string(),
                    offer_id,
                    taker_pubkey,
                    maker_pubkey,
                    timestamp: dm.timestamp,
                    message,
                    status: RequestStatus::Pending,
                    invite_link: None,
                    invite_expires_at: None,
                    updated_at: dm.timestamp,
                    proof_of_funds,
                };

                self.request_cache
                    .write()
                    .await
                    .insert(request.id.clone(), request.clone());

                return Ok(request);
            }
        }

        Err(BridgeError::SessionNotFound(format!(
            "Request {} not found in cache or DMs",
            request_id
        )))
    }

    /// Helper to provide a lightweight offer snapshot for request listings
    async fn offer_snapshot(&mut self, offer_id: &str) -> Option<OfferSummary> {
        if let Some(offer) = {
            let cache = self.offer_cache.read().await;
            cache.get(offer_id).cloned()
        } {
            return Some(OfferSummary::from(&offer));
        }

        match self.get_offer(offer_id).await {
            Ok(offer) => Some(OfferSummary::from(offer)),
            Err(e) => {
                log::debug!(
                    "Unable to load offer {} for trade request summary: {}",
                    offer_id,
                    e
                );
                None
            }
        }
    }

    /// Force refresh offers from Nostr (bypasses cache)
    pub async fn force_refresh_offers(&mut self) -> Result<(), BridgeError> {
        log::info!(
            "Force refreshing offers from Nostr relays for network: {}",
            self.network
        );

        // Fetch offers filtered by network to compartmentalize chains
        let filters = OfferFilters {
            network: Some(self.network.clone()),
            ..Default::default()
        };
        let offers = self.nostr_client.query_offers(filters).await?;

        // Update cache
        let mut cache = self.offer_cache.write().await;
        cache.clear();
        for offer in offers {
            cache.insert(offer.id.clone(), offer);
        }

        // Update refresh timestamp
        *self.last_offer_refresh.write().await = Instant::now();

        log::info!(
            "Force refreshed {} offers from Nostr for network {}",
            cache.len(),
            self.network
        );

        Ok(())
    }

    /// Refresh offers from Nostr if cache is stale (>5min)
    async fn refresh_offers_if_needed(&mut self) -> Result<(), BridgeError> {
        let last_refresh = *self.last_offer_refresh.read().await;

        // Refresh every 5 minutes
        if last_refresh.elapsed() < std::time::Duration::from_secs(300) {
            return Ok(()); // Cache still fresh
        }

        log::info!(
            "Refreshing offers from Nostr (cache stale) for network: {}",
            self.network
        );

        // Fetch offers filtered by network to compartmentalize chains
        let filters = OfferFilters {
            network: Some(self.network.clone()),
            ..Default::default()
        };
        let offers = self.nostr_client.query_offers(filters).await?;

        // Update cache
        let mut cache = self.offer_cache.write().await;
        cache.clear();
        for offer in offers {
            cache.insert(offer.id.clone(), offer);
        }

        // Update refresh timestamp
        *self.last_offer_refresh.write().await = Instant::now();

        log::info!(
            "Refreshed {} offers for network {}",
            cache.len(),
            self.network
        );

        Ok(())
    }

    // ==================== DISCUSSION METHODS ====================

    /// Publish a model-scoped discussion post.
    pub async fn post_discussion(
        &mut self,
        mut post: DiscussionPost,
    ) -> Result<DiscussionPost, BridgeError> {
        if post.network != self.network {
            return Err(BridgeError::InvalidCommand(format!(
                "Discussion network mismatch: post={}, manager={}",
                post.network, self.network
            )));
        }

        let actual_pubkey = self.nostr_client.get_public_key();
        if post.author_pubkey != actual_pubkey {
            log::warn!(
                "Overwriting discussion author pubkey: claimed={}, actual={}",
                post.author_pubkey,
                actual_pubkey
            );
            post.author_pubkey = actual_pubkey;
        }

        post.validate()
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid discussion post: {}", e)))?;

        let event_id = self.nostr_client.publish_discussion_post(&post).await?;
        post.post_id = event_id;
        post.created_at = chrono::Utc::now().timestamp().max(0) as u64;

        self.merge_discussion_posts(vec![post.clone()]).await?;

        self.discussion_scope_refresh
            .write()
            .await
            .insert(post.scope_key(), Instant::now());

        Ok(post)
    }

    /// List discussion posts for a specific scope, oldest first.
    pub async fn list_discussion(
        &mut self,
        scope_type: String,
        scope_id: String,
        since: Option<u64>,
        limit: Option<usize>,
    ) -> Result<DiscussionListResult, BridgeError> {
        let scope = DiscussionScope::from_str(&scope_type)
            .map_err(|e| BridgeError::InvalidCommand(e.to_string()))?;
        validate_scope(&scope, &scope_id)
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid discussion scope: {}", e)))?;
        let scope_key = build_scope_key(&scope, &scope_id);

        let refresh_result = self.refresh_discussion_if_needed(&scope_key, since).await;
        let mut stale = false;
        let mut refresh_error = None;

        let mut posts: Vec<DiscussionPost> = {
            let cache = self.discussion_cache.read().await;
            cache
                .values()
                .filter(|post| {
                    post.network == self.network
                        && post.scope_type == scope
                        && post.scope_id == scope_id
                        && since.map(|ts| post.created_at >= ts).unwrap_or(true)
                })
                .cloned()
                .collect()
        };

        if let Err(err) = refresh_result {
            if posts.is_empty() {
                return Err(err);
            }

            stale = true;
            refresh_error = Some(err.to_string());
            log::warn!(
                "Returning cached discussion posts for {} after refresh failure: {}",
                scope_key,
                err
            );
        }

        posts.sort_by(|a, b| a.created_at.cmp(&b.created_at));

        if let Some(limit) = limit {
            if posts.len() > limit {
                posts = posts.split_off(posts.len() - limit);
            }
        }

        Ok(DiscussionListResult {
            posts,
            stale,
            refresh_error,
        })
    }

    /// Force a Nostr refresh for one discussion scope.
    pub async fn force_refresh_discussion(
        &mut self,
        scope_type: String,
        scope_id: String,
        since: Option<u64>,
    ) -> Result<(), BridgeError> {
        let scope = DiscussionScope::from_str(&scope_type)
            .map_err(|e| BridgeError::InvalidCommand(e.to_string()))?;
        validate_scope(&scope, &scope_id)
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid discussion scope: {}", e)))?;
        let scope_key = build_scope_key(&scope, &scope_id);
        self.refresh_discussion_scope(&scope_key, since, true).await
    }

    pub async fn list_discussion_scopes(
        &mut self,
        since: Option<u64>,
        limit: Option<usize>,
        force_refresh: bool,
    ) -> Result<Vec<DiscussionScopeSummary>, BridgeError> {
        if force_refresh {
            let posts = self
                .nostr_client
                .query_discussion_posts(None, Some(&self.network), since)
                .await?;
            self.merge_discussion_posts(posts).await?;
        }

        let cache = self.discussion_cache.read().await;
        let mut scopes: HashMap<String, DiscussionScopeSummary> = HashMap::new();

        for post in cache.values() {
            if post.network != self.network {
                continue;
            }
            if since.map(|ts| post.created_at < ts).unwrap_or(false) {
                continue;
            }

            let key = post.scope_key();
            let preview = if post.content.len() > 96 {
                format!("{}...", &post.content[..96])
            } else {
                post.content.clone()
            };

            let entry = scopes.entry(key).or_insert_with(|| DiscussionScopeSummary {
                scope_type: post.scope_type.as_str().to_string(),
                scope_id: post.scope_id.clone(),
                latest_created_at: post.created_at,
                post_count: 0,
                latest_post_id: post.post_id.clone(),
                latest_content_preview: preview.clone(),
                model_identifier: post.model_identifier.clone(),
            });

            entry.post_count += 1;
            if post.created_at >= entry.latest_created_at {
                entry.latest_created_at = post.created_at;
                entry.latest_post_id = post.post_id.clone();
                entry.latest_content_preview = preview;
                entry.model_identifier = post.model_identifier.clone();
            }
        }

        let mut out: Vec<DiscussionScopeSummary> = scopes.into_values().collect();
        out.sort_by(|a, b| {
            b.latest_created_at
                .cmp(&a.latest_created_at)
                .then_with(|| a.scope_type.cmp(&b.scope_type))
                .then_with(|| a.scope_id.cmp(&b.scope_id))
        });

        if let Some(limit) = limit {
            out.truncate(limit);
        }

        Ok(out)
    }

    async fn refresh_discussion_if_needed(
        &mut self,
        scope_key: &str,
        since: Option<u64>,
    ) -> Result<(), BridgeError> {
        let scope_refresh = self.discussion_scope_refresh.read().await;
        if let Some(last_refresh) = scope_refresh.get(scope_key) {
            if last_refresh.elapsed() < std::time::Duration::from_secs(25) {
                return Ok(());
            }
        }
        drop(scope_refresh);

        self.refresh_discussion_scope(scope_key, since, false).await
    }

    async fn refresh_discussion_scope(
        &mut self,
        scope_key: &str,
        since: Option<u64>,
        force: bool,
    ) -> Result<(), BridgeError> {
        if force {
            log::info!(
                "Force refreshing discussion scope {} from Nostr for network {} via relays {:?}",
                scope_key,
                self.network,
                self.get_relay_urls()
            );
        } else {
            log::info!(
                "Refreshing discussion scope {} from Nostr for network {} via relays {:?}",
                scope_key,
                self.network,
                self.get_relay_urls()
            );
        }

        let posts = self
            .nostr_client
            .query_discussion_posts(Some(scope_key), Some(&self.network), since)
            .await?;
        let post_count = posts.len();

        self.merge_discussion_posts(posts).await?;

        self.discussion_scope_refresh
            .write()
            .await
            .insert(scope_key.to_string(), Instant::now());

        log::info!(
            "Refreshed {} discussion posts for scope {} on network {}",
            post_count,
            scope_key,
            self.network
        );

        Ok(())
    }

    async fn merge_discussion_posts(
        &mut self,
        posts: Vec<DiscussionPost>,
    ) -> Result<(), BridgeError> {
        if posts.is_empty() {
            return Ok(());
        }

        {
            let mut cache = self.discussion_cache.write().await;
            for post in &posts {
                let payload = serde_json::to_vec(post).map_err(|e| {
                    BridgeError::InvalidCommand(format!(
                        "Failed to serialize discussion post {}: {}",
                        post.post_id, e
                    ))
                })?;

                self.discussion_posts_db
                    .insert(post.post_id.as_bytes(), payload)
                    .map_err(|e| {
                        BridgeError::InvalidCommand(format!(
                            "Failed to persist discussion post {}: {}",
                            post.post_id, e
                        ))
                    })?;

                cache.insert(post.post_id.clone(), post.clone());
            }
        }

        self.discussion_posts_db.flush().map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to flush discussion posts DB: {}", e))
        })?;

        Ok(())
    }

    // ==================== GOVERNANCE METHODS ====================

    /// Publish a governance proposal to the bulletin board
    ///
    /// # Arguments
    ///
    /// * `proposal` - Governance proposal to publish
    /// * `rate_limit_secs` - Optional rate limit in seconds (default: 3600 = 1 hour)
    ///
    /// # Returns
    ///
    /// Proposal ID
    ///
    /// # Rate Limiting
    ///
    /// Only one active (non-expired) proposal per asset is allowed.
    /// After a proposal expires, a configurable cooldown period must pass before
    /// a new proposal can be submitted for the same asset.
    pub async fn publish_governance(
        &mut self,
        mut proposal: GovernanceProposal,
        rate_limit_secs: Option<u64>,
    ) -> Result<String, BridgeError> {
        // Validate proposal structure. For private flows, validate the sanitized view that will
        // actually be published (icu_text/template_psbt stripped).
        let mut validation_candidate = proposal.clone();
        if matches!(
            validation_candidate.flow_type,
            crate::bulletin_board::governance::FlowType::Private
        ) {
            validation_candidate.icu_text = None;
            validation_candidate.template_psbt = None;
        }
        validation_candidate.validate().map_err(|e| {
            BridgeError::InvalidCommand(format!("Invalid governance proposal: {}", e))
        })?;

        // SECURITY: Overwrite issuer_nostr_pubkey with the bridge's actual signing key
        // This prevents malicious issuers from claiming another identity
        let actual_pubkey = self.nostr_client.get_public_key();
        if proposal.issuer_nostr_pubkey != actual_pubkey {
            log::warn!(
                "Overwriting issuer_nostr_pubkey: claimed={}, actual={}",
                proposal.issuer_nostr_pubkey,
                actual_pubkey
            );
            proposal.issuer_nostr_pubkey = actual_pubkey;
        }

        // SECURITY: Verify ICU ownership via BIP-322 attestation
        // The attestation must prove control of the ICU address
        if !proposal.verify_icu_attestation_message() {
            return Err(BridgeError::InvalidCommand(format!(
                "BIP-322 attestation message format invalid for proposal {}",
                proposal.proposal_id
            )));
        }

        // Note: Full cryptographic BIP-322 verification should be done by the RPC layer
        // before the proposal reaches this point (see cosign_publish_governance in cosign.cpp)

        // Check rate limiting
        let rate_limit = rate_limit_secs.unwrap_or(3600); // Default: 1 hour
        let now = chrono::Utc::now().timestamp() as u64;

        {
            let asset_proposals = self.asset_last_proposal.read().await;
            if let Some(last_ts) = asset_proposals.get(&proposal.asset_id) {
                if now - last_ts < rate_limit {
                    let remaining = rate_limit - (now - last_ts);
                    return Err(BridgeError::InvalidCommand(format!(
                        "Rate limit: cannot publish new proposal for asset {} for {} more seconds",
                        proposal.asset_id, remaining
                    )));
                }
            }
        }

        // Check for existing active proposals for this asset
        {
            let cache = self.governance_cache.read().await;
            for existing in cache.values() {
                if existing.asset_id == proposal.asset_id && !existing.is_expired() {
                    return Err(BridgeError::InvalidCommand(format!(
                        "Active proposal {} already exists for asset {}. Wait for expiry or use a different asset.",
                        existing.proposal_id, proposal.asset_id
                    )));
                }
            }
        }

        // SECURITY: For private proposals, strip sensitive fields before publishing to Nostr
        // The full proposal is cached locally and only sent via encrypted DM after ownership verification
        let proposal_to_publish = if matches!(
            proposal.flow_type,
            crate::bulletin_board::governance::FlowType::Private
        ) {
            log::info!(
                "Private governance proposal {}: caching sensitive fields before Nostr publish",
                proposal.proposal_id
            );

            // Cache the private payload with computed hashes
            let private_data = PrivateProposalData {
                proposal_id: proposal.proposal_id.clone(),
                icu_text: proposal.icu_text.clone(),
                template_psbt: proposal.template_psbt.clone(),
                witness_bundle: proposal.witness_bundle.clone(),
                canonical_icu_hash: proposal.canonical_icu_hash.clone(),
                template_psbt_hash: Some(proposal.template_psbt_hash.clone()),
                witness_bundle_hash: proposal.witness_bundle_hash.clone(),
            };

            // Store in persistent DB
            let payload_json = serde_json::to_vec(&private_data).map_err(|e| {
                BridgeError::InvalidCommand(format!("Failed to serialize private payload: {}", e))
            })?;

            self.private_payloads_db
                .insert(proposal.proposal_id.as_bytes(), payload_json)
                .map_err(|e| {
                    BridgeError::InvalidCommand(format!(
                        "Failed to store private payload in DB: {}",
                        e
                    ))
                })?;

            // Strip sensitive fields from the public version
            let mut sanitized = proposal.clone();
            sanitized.icu_text = None;
            sanitized.template_psbt = None;
            sanitized.witness_bundle = None;
            sanitized.canonical_icu_hash = None;

            // DEFENSIVE: Validate sanitization succeeded
            if sanitized.icu_text.is_some()
                || sanitized.template_psbt.is_some()
                || sanitized.witness_bundle.is_some()
                || sanitized.canonical_icu_hash.is_some()
            {
                return Err(BridgeError::InvalidCommand(format!(
                    "CRITICAL: Failed to sanitize private proposal {} - sensitive fields still present after strip",
                    proposal.proposal_id
                )));
            }

            log::info!("Private proposal {} sanitized: stripped icu_text={}, template_psbt={}, witness_bundle={}, canonical_icu_hash={}",
                proposal.proposal_id,
                proposal.icu_text.is_some(),
                proposal.template_psbt.is_some(),
                proposal.witness_bundle.is_some(),
                proposal.canonical_icu_hash.is_some()
            );

            sanitized
        } else {
            // Public proposal: publish as-is
            proposal.clone()
        };

        // Serialize the (potentially sanitized) proposal
        let proposal_json = serde_json::to_string(&proposal_to_publish).map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to serialize proposal: {}", e))
        })?;

        // Publish to Nostr
        let event_id = self
            .nostr_client
            .publish_governance_note(&proposal_json)
            .await?;

        // Update the ORIGINAL proposal (not sanitized) with event ID for local cache
        proposal.nostr_event_id = Some(event_id);

        // Cache locally
        let proposal_id = proposal.proposal_id.clone();
        let asset_id = proposal.asset_id.clone();

        self.governance_cache
            .write()
            .await
            .insert(proposal_id.clone(), proposal);

        // Update rate limit tracker
        self.asset_last_proposal.write().await.insert(asset_id, now);

        log::info!("Published governance proposal: {}", proposal_id);

        Ok(proposal_id)
    }

    /// List governance proposals from bulletin board
    ///
    /// # Arguments
    ///
    /// * `asset_id` - Optional asset ID filter (if None, returns all proposals)
    /// * `include_expired` - Whether to include expired proposals (default: false)
    ///
    /// # Returns
    ///
    /// List of governance proposal summaries
    pub async fn list_governance(
        &mut self,
        asset_id: Option<String>,
        include_expired: bool,
    ) -> Result<Vec<GovernanceProposal>, BridgeError> {
        // Refresh cache if stale (>5min)
        self.refresh_governance_if_needed().await?;

        // Get proposals from cache
        let cache = self.governance_cache.read().await;
        let mut proposals: Vec<GovernanceProposal> = cache.values().cloned().collect();

        // Apply filters
        proposals.retain(|proposal| {
            // Filter by asset_id if specified
            if let Some(ref filter_asset_id) = asset_id {
                if &proposal.asset_id != filter_asset_id {
                    return false;
                }
            }

            // Filter expired proposals
            if !include_expired && proposal.is_expired() {
                return false;
            }

            true
        });

        // Sort by creation time (newest first)
        proposals.sort_by(|a, b| b.created_at.cmp(&a.created_at));

        // Merge received proposal responses from persistent cache (holder-side)
        // This ensures GRANTED state persists across refreshes
        for proposal in &mut proposals {
            if let Ok(Some(response_bytes)) = self
                .received_proposals_db
                .get(proposal.proposal_id.as_bytes())
            {
                if let Ok(response) =
                    serde_json::from_slice::<GovernanceProposalResponse>(&response_bytes)
                {
                    // Merge private fields from cached response
                    proposal.icu_text = Some(response.icu_text);
                    proposal.template_psbt = Some(response.template_psbt);
                    proposal.canonical_icu_hash = Some(response.canonical_icu_hash);
                    proposal.template_psbt_hash = response.template_psbt_hash;

                    // Optional fields
                    if let Some(witness_bundle) = response.witness_bundle {
                        proposal.witness_bundle = Some(witness_bundle);
                    }
                    if let Some(witness_bundle_hash) = response.witness_bundle_hash {
                        proposal.witness_bundle_hash = Some(witness_bundle_hash);
                    }

                    log::debug!(
                        "Merged cached proposal response for {}",
                        proposal.proposal_id
                    );
                }
            }
        }

        // Return full proposals (not summaries) so UI has access to
        // icu_attestation, icu_text, and other verification fields
        Ok(proposals)
    }

    /// Get a specific governance proposal by ID
    ///
    /// # Arguments
    ///
    /// * `proposal_id` - Proposal ID to fetch
    ///
    /// # Returns
    ///
    /// Full governance proposal if found
    pub async fn get_governance(
        &mut self,
        proposal_id: &str,
    ) -> Result<GovernanceProposal, BridgeError> {
        // First check cache
        {
            let cache = self.governance_cache.read().await;
            if let Some(proposal) = cache.get(proposal_id) {
                return Ok(proposal.clone());
            }
        }

        // Cache miss - refresh from Nostr
        log::debug!("Proposal {} not in cache, querying Nostr", proposal_id);
        self.refresh_governance_if_needed().await?;

        // Try again after refresh
        let cache = self.governance_cache.read().await;
        let mut proposal = cache.get(proposal_id).cloned().ok_or_else(|| {
            BridgeError::SessionNotFound(format!("Governance proposal not found: {}", proposal_id))
        })?;

        // Merge received proposal response from persistent cache if available
        if let Ok(Some(response_bytes)) = self.received_proposals_db.get(proposal_id.as_bytes()) {
            if let Ok(response) =
                serde_json::from_slice::<GovernanceProposalResponse>(&response_bytes)
            {
                proposal.icu_text = Some(response.icu_text);
                proposal.template_psbt = Some(response.template_psbt);
                proposal.canonical_icu_hash = Some(response.canonical_icu_hash);
                proposal.template_psbt_hash = response.template_psbt_hash;

                // Optional fields
                if let Some(witness_bundle) = response.witness_bundle {
                    proposal.witness_bundle = Some(witness_bundle);
                }
                if let Some(witness_bundle_hash) = response.witness_bundle_hash {
                    proposal.witness_bundle_hash = Some(witness_bundle_hash);
                }

                log::debug!("Merged cached proposal response for {}", proposal_id);
            }
        }

        Ok(proposal)
    }

    /// Force refresh governance proposals from Nostr (bypasses cache)
    #[allow(dead_code)] // Exposed for manual refresh in Qt (PR2)
    pub async fn force_refresh_governance(&mut self) -> Result<(), BridgeError> {
        log::info!("Force refreshing governance proposals from Nostr relays");

        // Fetch all governance proposals from Nostr
        let proposals = self.nostr_client.query_governance_notes().await?;

        // Update cache
        let mut cache = self.governance_cache.write().await;
        cache.clear();
        for proposal in proposals {
            cache.insert(proposal.proposal_id.clone(), proposal);
        }

        // Update refresh timestamp
        *self.last_governance_refresh.write().await = Instant::now();

        log::info!(
            "Force refreshed {} governance proposals from Nostr",
            cache.len()
        );

        Ok(())
    }

    /// Refresh governance proposals from Nostr if cache is stale (>5min)
    async fn refresh_governance_if_needed(&mut self) -> Result<(), BridgeError> {
        let last_refresh = *self.last_governance_refresh.read().await;

        // Refresh every 5 minutes
        if last_refresh.elapsed() < std::time::Duration::from_secs(300) {
            return Ok(()); // Cache still fresh
        }

        log::info!("Refreshing governance proposals from Nostr (cache stale)");

        // Fetch all governance proposals from Nostr
        let proposals = self.nostr_client.query_governance_notes().await?;

        // Update cache
        let mut cache = self.governance_cache.write().await;
        cache.clear();
        for proposal in proposals {
            cache.insert(proposal.proposal_id.clone(), proposal);
        }

        // Update refresh timestamp
        *self.last_governance_refresh.write().await = Instant::now();

        log::info!("Refreshed {} governance proposals", cache.len());

        Ok(())
    }

    /// Publish a governance ballot (holder's vote) to the bulletin board
    ///
    /// # Arguments
    ///
    /// * `ballot` - Ballot to publish
    ///
    /// # Returns
    ///
    /// Ballot identifier (computed from signed_psbt hash)
    pub async fn publish_ballot(
        &mut self,
        mut ballot: GovernanceBallot,
    ) -> Result<String, BridgeError> {
        // Validate ballot structure
        ballot.validate().map_err(|e| {
            BridgeError::InvalidCommand(format!("Invalid governance ballot: {}", e))
        })?;

        // Verify that the referenced proposal exists and is not expired
        {
            let cache = self.governance_cache.read().await;
            let proposal = cache.get(&ballot.proposal_id).ok_or_else(|| {
                BridgeError::InvalidCommand(format!(
                    "Proposal {} not found. Refresh governance first.",
                    ballot.proposal_id
                ))
            })?;

            if proposal.is_expired() {
                return Err(BridgeError::InvalidCommand(format!(
                    "Cannot vote on expired proposal {}",
                    ballot.proposal_id
                )));
            }

            // Verify asset_id matches
            if proposal.asset_id != ballot.asset_id {
                return Err(BridgeError::InvalidCommand(format!(
                    "Asset ID mismatch: proposal has {}, ballot has {}",
                    proposal.asset_id, ballot.asset_id
                )));
            }
        }

        // Publish ballot to Nostr
        let ballot_json = serde_json::to_string(&ballot).map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to serialize ballot: {}", e))
        })?;

        let event_id = self
            .nostr_client
            .publish_governance_ballot_note(&ballot_json)
            .await?;

        ballot.nostr_event_id = Some(event_id.clone());

        // Compute ballot_id from signed_psbt hash (for tracking)
        let ballot_id = {
            use sha2::{Digest, Sha256};
            let mut hasher = Sha256::new();
            hasher.update(ballot.signed_psbt.as_bytes());
            format!("{:x}", hasher.finalize())
        };

        // Cache the ballot
        let proposal_id = ballot.proposal_id.clone();
        let mut cache = self.ballot_cache.write().await;
        cache
            .entry(proposal_id.clone())
            .or_insert_with(Vec::new)
            .push(ballot);

        log::info!(
            "Published ballot {} for proposal {} with event_id {}",
            ballot_id,
            proposal_id,
            event_id
        );

        Ok(ballot_id)
    }

    /// List ballots (holder votes) for a governance proposal
    ///
    /// # Arguments
    ///
    /// * `proposal_id` - Proposal ID to get ballots for
    ///
    /// # Returns
    ///
    /// List of ballots for the proposal
    pub async fn list_ballots(
        &mut self,
        proposal_id: String,
    ) -> Result<Vec<GovernanceBallot>, BridgeError> {
        // Verify proposal exists
        {
            let cache = self.governance_cache.read().await;
            if !cache.contains_key(&proposal_id) {
                return Err(BridgeError::InvalidCommand(format!(
                    "Proposal {} not found. Refresh governance first.",
                    proposal_id
                )));
            }
        }

        // For now, return ballots from cache
        // TODO: In PR3, implement Nostr queries for ballots with NIP-04 encryption
        let cache = self.ballot_cache.read().await;
        let ballots = cache.get(&proposal_id).cloned().unwrap_or_default();

        log::info!(
            "Returning {} cached ballots for proposal {}",
            ballots.len(),
            proposal_id
        );

        Ok(ballots)
    }

    // ==================== PRIVATE GOVERNANCE ====================

    /// Retrieve cached private governance proposal data
    ///
    /// # Arguments
    ///
    /// * `proposal_id` - Proposal ID
    ///
    /// # Returns
    ///
    /// Private payload data (ICU text, template PSBT, witness bundle)
    ///
    /// # Errors
    ///
    /// Returns error if proposal_id not found in cache
    pub async fn get_private_payload(
        &self,
        proposal_id: &str,
    ) -> Result<PrivateProposalData, BridgeError> {
        let payload_bytes = self.private_payloads_db
            .get(proposal_id.as_bytes())
            .map_err(|e| {
                BridgeError::InvalidCommand(format!("Failed to query private payloads DB: {}", e))
            })?
            .ok_or_else(|| BridgeError::InvalidCommand(format!(
                "Private payload not found for proposal {}. Either not a private proposal or not yet published by this issuer.",
                proposal_id
            )))?;

        let private_data: PrivateProposalData =
            serde_json::from_slice(&payload_bytes).map_err(|e| {
                BridgeError::InvalidCommand(format!("Failed to deserialize private payload: {}", e))
            })?;

        Ok(private_data)
    }

    /// Request access to a private governance proposal (Holder → Issuer DM)
    ///
    /// # Arguments
    ///
    /// * `request` - Access request with BIP-322 ownership proof
    ///
    /// # Returns
    ///
    /// Request ID for tracking
    pub async fn request_private_proposal(
        &mut self,
        request: GovernanceAccessRequest,
        issuer_nostr_pubkey: String,
    ) -> Result<String, BridgeError> {
        log::info!(
            "DEBUG: request_private_proposal called for proposal_id: {}, issuer_pubkey: {}",
            request.proposal_id,
            issuer_nostr_pubkey
        );

        // Validate request structure
        request.validate().map_err(|e| {
            log::error!("DEBUG: Request validation failed: {}", e);
            BridgeError::InvalidCommand(format!("Invalid access request: {}", e))
        })?;

        log::info!("DEBUG: Request validated successfully");

        // NOTE: No cache lookup needed - all data comes from UI which already has the proposal
        {
            // Get issuer pubkey for DM routing (passed from caller)
            let issuer_pubkey = issuer_nostr_pubkey;

            // Serialize request as JSON
            let request_json = serde_json::to_string(&request).map_err(|e| {
                log::error!("DEBUG: Failed to serialize request: {}", e);
                BridgeError::InvalidCommand(format!("Failed to serialize request: {}", e))
            })?;

            log::info!(
                "DEBUG: Request serialized, calling send_dm to issuer: {}",
                issuer_pubkey
            );

            // Send encrypted DM to issuer
            let dm_event_id = self
                .nostr_client
                .send_dm(&issuer_pubkey, &request_json)
                .await?;

            log::info!("DEBUG: DM sent successfully, event_id: {}", dm_event_id);

            log::info!(
                "Sent private proposal access request for {} to issuer {} (DM event: {})",
                request.proposal_id,
                issuer_pubkey,
                dm_event_id
            );

            // Compute request_id for tracking
            let request_id = format!("{}:{}", request.proposal_id, request.holder_nostr_pubkey);

            Ok(request_id)
        }
    }

    /// Send governance proposal response to holder (Issuer → Holder DM)
    ///
    /// # Arguments
    ///
    /// * `holder_pubkey` - Holder's Nostr public key
    /// * `response` - Full proposal details including ICU text
    ///
    /// # Returns
    ///
    /// DM event ID
    pub async fn send_proposal_response(
        &mut self,
        holder_pubkey: &str,
        response: GovernanceProposalResponse,
    ) -> Result<String, BridgeError> {
        // Serialize response as JSON
        let response_json = serde_json::to_string(&response).map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to serialize response: {}", e))
        })?;

        // Send encrypted DM to holder
        let dm_event_id = self
            .nostr_client
            .send_dm(holder_pubkey, &response_json)
            .await?;

        log::info!(
            "Sent private proposal response for {} to holder {} (DM event: {})",
            response.proposal_id,
            holder_pubkey,
            dm_event_id
        );

        Ok(dm_event_id)
    }

    /// Submit governance ballot via DM (Holder → Issuer DM)
    ///
    /// # Arguments
    ///
    /// * `ballot_dm` - Ballot submission with signed PSBT
    ///
    /// # Returns
    ///
    /// Ballot ID for tracking
    pub async fn send_governance_ballot_dm(
        &mut self,
        ballot_dm: GovernanceBallotDM,
    ) -> Result<String, BridgeError> {
        // Verify the proposal exists
        let issuer_pubkey = {
            let cache = self.governance_cache.read().await;
            let proposal = cache.get(&ballot_dm.proposal_id).ok_or_else(|| {
                BridgeError::InvalidCommand(format!(
                    "Proposal {} not found. Fetch governance first.",
                    ballot_dm.proposal_id
                ))
            })?;

            // Verify asset_id matches
            if proposal.asset_id != ballot_dm.asset_id {
                return Err(BridgeError::InvalidCommand(format!(
                    "Asset ID mismatch: proposal has {}, ballot has {}",
                    proposal.asset_id, ballot_dm.asset_id
                )));
            }

            proposal.issuer_nostr_pubkey.clone()
        };

        // Serialize ballot as JSON
        let ballot_json = serde_json::to_string(&ballot_dm).map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to serialize ballot: {}", e))
        })?;

        // Send encrypted DM to issuer
        let dm_event_id = self
            .nostr_client
            .send_dm(&issuer_pubkey, &ballot_json)
            .await?;

        // Compute ballot_id from signed_psbt hash
        let ballot_id = {
            use sha2::{Digest, Sha256};
            let mut hasher = Sha256::new();
            hasher.update(ballot_dm.signed_psbt.as_bytes());
            format!("{:x}", hasher.finalize())
        };

        log::info!(
            "Sent private ballot {} for proposal {} to issuer {} (DM event: {})",
            ballot_id,
            ballot_dm.proposal_id,
            issuer_pubkey,
            dm_event_id
        );

        Ok(ballot_id)
    }

    /// Send ballot receipt confirmation (Issuer → Holder DM)
    ///
    /// # Arguments
    ///
    /// * `holder_pubkey` - Holder's Nostr public key
    /// * `receipt` - Receipt with quorum status
    ///
    /// # Returns
    ///
    /// DM event ID
    ///
    /// TODO: This should be called automatically when processing ballot DMs on issuer side
    #[allow(dead_code)]
    pub async fn send_ballot_receipt(
        &mut self,
        holder_pubkey: &str,
        receipt: GovernanceBallotReceipt,
    ) -> Result<String, BridgeError> {
        // Serialize receipt as JSON
        let receipt_json = serde_json::to_string(&receipt).map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to serialize receipt: {}", e))
        })?;

        // Send encrypted DM to holder
        let dm_event_id = self
            .nostr_client
            .send_dm(holder_pubkey, &receipt_json)
            .await?;

        log::info!(
            "Sent ballot receipt {} for proposal {} to holder {} (DM event: {})",
            receipt.ballot_id,
            receipt.proposal_id,
            holder_pubkey,
            dm_event_id
        );

        Ok(dm_event_id)
    }

    /// Check and store governance DM envelope for replay protection
    ///
    /// Returns Ok(prev_hash) if envelope is valid and not replayed,
    /// Err if replay detected or validation fails
    fn check_and_store_envelope(
        &self,
        envelope: &crate::bulletin_board::governance::GovernanceDMEnvelope,
    ) -> Result<Option<String>, BridgeError> {
        use std::time::{SystemTime, UNIX_EPOCH};

        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        // Build replay cache key: {proposal_id}:{sequence}
        let replay_key = format!("{}:{}", envelope.proposal_id, envelope.sequence);

        // Check if we've already processed this sequence for this proposal
        if let Ok(Some(stored_hash)) = self.governance_replay_db.get(replay_key.as_bytes()) {
            let stored_hash_str = String::from_utf8_lossy(&stored_hash);
            let current_hash = envelope.compute_hash();

            if stored_hash_str == current_hash {
                // Exact duplicate - replay detected
                return Err(BridgeError::InvalidCommand(format!(
                    "Replay detected: envelope {}:{} already processed",
                    envelope.proposal_id, envelope.sequence
                )));
            } else {
                // Different envelope with same proposal_id:sequence - conflict!
                return Err(BridgeError::InvalidCommand(format!(
                    "Sequence conflict: envelope {}:{} exists with different hash",
                    envelope.proposal_id, envelope.sequence
                )));
            }
        }

        // Get expected prev_hash from previous sequence (if not first message)
        let expected_prev_hash = if envelope.sequence > 1 {
            let prev_key = format!("{}:{}", envelope.proposal_id, envelope.sequence - 1);
            if let Ok(Some(prev_hash_bytes)) = self.governance_replay_db.get(prev_key.as_bytes()) {
                Some(String::from_utf8_lossy(&prev_hash_bytes).to_string())
            } else {
                return Err(BridgeError::InvalidCommand(format!(
                    "Missing previous message: expected sequence {} before {}",
                    envelope.sequence - 1,
                    envelope.sequence
                )));
            }
        } else {
            None
        };

        // Validate envelope (timestamp, expiry, sequence, prev_hash chain)
        envelope
            .validate(now, expected_prev_hash.as_deref())
            .map_err(|e| {
                BridgeError::InvalidCommand(format!("Envelope validation failed: {}", e))
            })?;

        // Store envelope hash in replay cache
        let current_hash = envelope.compute_hash();
        self.governance_replay_db
            .insert(replay_key.as_bytes(), current_hash.as_bytes())
            .map_err(|e| {
                BridgeError::InvalidCommand(format!("Failed to store replay state: {}", e))
            })?;

        log::info!(
            "Stored envelope {}:{} with hash {}",
            envelope.proposal_id,
            envelope.sequence,
            current_hash
        );

        Ok(expected_prev_hash)
    }

    /// Query governance conversation session status
    ///
    /// Returns the highest envelope sequence stored for a given proposal,
    /// indicating the current conversation state (1=requested, 2=granted, 3=voted, 4=acknowledged).
    ///
    /// # Arguments
    ///
    /// * `proposal_id` - Proposal ID to query
    ///
    /// # Returns
    ///
    /// Option<u64> - Highest sequence number, or None if no session exists
    #[allow(dead_code)]
    pub fn get_session_status(&self, proposal_id: &str) -> Option<u64> {
        let mut max_sequence = None;

        // Scan all keys with prefix "{proposal_id}:"
        let prefix = format!("{}:", proposal_id);
        for (key, _value) in self
            .governance_replay_db
            .scan_prefix(prefix.as_bytes())
            .flatten()
        {
            // Parse sequence from key "{proposal_id}:{sequence}"
            if let Ok(key_str) = String::from_utf8(key.to_vec()) {
                if let Some(seq_str) = key_str.split(':').nth(1) {
                    if let Ok(seq) = seq_str.parse::<u64>() {
                        max_sequence = Some(max_sequence.unwrap_or(0).max(seq));
                    }
                }
            }
        }

        max_sequence
    }

    /// Process incoming governance DMs and return parsed messages
    ///
    /// # Arguments
    ///
    /// * `since` - Optional timestamp to fetch DMs from (default: last hour)
    ///
    /// # Returns
    ///
    /// Tuple of (access_requests, proposal_responses, ballot_dms, ballot_receipts)
    ///
    /// # Note
    ///
    /// This method implements persistent replay protection via sled DB.
    /// Each envelope is validated for sequence integrity and timing constraints.
    pub async fn process_governance_dms(
        &mut self,
        since: Option<u64>,
    ) -> Result<
        (
            Vec<(GovernanceAccessRequest, String)>, // (request, from_pubkey)
            Vec<(GovernanceProposalResponse, String)>, // (response, from_pubkey)
            Vec<(GovernanceBallotDM, String)>,      // (ballot, from_pubkey)
            Vec<(GovernanceBallotReceipt, String)>, // (receipt, from_pubkey)
        ),
        BridgeError,
    > {
        // Fetch DMs
        let dms = self.nostr_client.fetch_dms(since).await?;

        let mut access_requests = Vec::new();
        let mut proposal_responses = Vec::new();
        let mut ballot_dms = Vec::new();
        let mut ballot_receipts = Vec::new();

        for dm in dms {
            log::info!(
                "Processing DM from {}, content length: {}",
                dm.from_pubkey,
                dm.content.len()
            );

            // First, try to parse as envelope (new protocol)
            if let Ok(envelope) = serde_json::from_str::<GovernanceDMEnvelope>(&dm.content) {
                log::debug!(
                    "Parsed envelope: type={:?}, proposal_id={}, sequence={}",
                    envelope.message_type,
                    envelope.proposal_id,
                    envelope.sequence
                );

                // Validate envelope and check for replay
                match self.check_and_store_envelope(&envelope) {
                    Ok(_) => {
                        log::info!(
                            "Envelope validated: {}:{}",
                            envelope.proposal_id,
                            envelope.sequence
                        );
                    }
                    Err(e) => {
                        log::warn!("Envelope validation failed: {}", e);
                        continue; // Skip invalid/replayed envelopes
                    }
                }

                // Parse inner payload based on message type
                match envelope.message_type {
                    GovernanceDMType::AccessRequest => {
                        match serde_json::from_str::<GovernanceAccessRequest>(&envelope.payload) {
                            Ok(request) => {
                                log::info!("AccessRequest for proposal: {}", request.proposal_id);
                                access_requests.push((request, dm.from_pubkey.clone()));
                            }
                            Err(e) => {
                                log::error!("Failed to parse AccessRequest payload: {}", e);
                            }
                        }
                    }
                    GovernanceDMType::ProposalResponse => {
                        match serde_json::from_str::<GovernanceProposalResponse>(&envelope.payload)
                        {
                            Ok(response) => {
                                log::info!(
                                    "ProposalResponse for proposal: {}",
                                    response.proposal_id
                                );

                                // Persist to holder-side cache
                                if let Ok(json) = serde_json::to_vec(&response) {
                                    if let Err(e) = self
                                        .received_proposals_db
                                        .insert(response.proposal_id.as_bytes(), json)
                                    {
                                        log::error!("Failed to cache response: {}", e);
                                    } else {
                                        log::info!("Cached response for {}", response.proposal_id);
                                    }
                                }

                                proposal_responses.push((response, dm.from_pubkey.clone()));
                            }
                            Err(e) => {
                                log::error!("Failed to parse ProposalResponse payload: {}", e);
                            }
                        }
                    }
                    GovernanceDMType::Ballot => {
                        match serde_json::from_str::<GovernanceBallotDM>(&envelope.payload) {
                            Ok(ballot) => {
                                log::info!("Ballot for proposal: {}", ballot.proposal_id);
                                ballot_dms.push((ballot, dm.from_pubkey.clone()));
                            }
                            Err(e) => {
                                log::error!("Failed to parse Ballot payload: {}", e);
                            }
                        }
                    }
                    GovernanceDMType::BallotReceipt => {
                        match serde_json::from_str::<GovernanceBallotReceipt>(&envelope.payload) {
                            Ok(receipt) => {
                                log::info!("BallotReceipt for proposal: {}", receipt.proposal_id);
                                ballot_receipts.push((receipt, dm.from_pubkey.clone()));
                            }
                            Err(e) => {
                                log::error!("Failed to parse BallotReceipt payload: {}", e);
                            }
                        }
                    }
                }
                continue;
            }

            // Backwards compatibility: try direct parsing (legacy messages without envelope)
            log::debug!("Not an envelope, trying legacy direct parsing");

            let mut seen_cache = self.governance_dm_seen.write().await;

            // Try to parse as GovernanceAccessRequest (legacy)
            if let Ok(request) = serde_json::from_str::<GovernanceAccessRequest>(&dm.content) {
                let msg_id = {
                    use sha2::{Digest, Sha256};
                    let mut hasher = Sha256::new();
                    hasher.update(b"access_request");
                    hasher.update(request.proposal_id.as_bytes());
                    hasher.update(request.holder_nostr_pubkey.as_bytes());
                    hasher.update(request.requested_at.to_string().as_bytes());
                    if let Some(seq) = request.sequence {
                        hasher.update(seq.to_string().as_bytes());
                    }
                    format!("{:x}", hasher.finalize())
                };

                if seen_cache.contains(&msg_id) {
                    log::debug!("Skipping duplicate legacy access request");
                    continue;
                }

                seen_cache.insert(msg_id);
                log::info!("Legacy AccessRequest for proposal: {}", request.proposal_id);
                access_requests.push((request, dm.from_pubkey.clone()));
                continue;
            }

            // Try to parse as GovernanceProposalResponse (legacy)
            if let Ok(response) = serde_json::from_str::<GovernanceProposalResponse>(&dm.content) {
                let msg_id = {
                    use sha2::{Digest, Sha256};
                    let mut hasher = Sha256::new();
                    hasher.update(b"proposal_response");
                    hasher.update(response.proposal_id.as_bytes());
                    hasher.update(response.responded_at.to_string().as_bytes());
                    if let Some(seq) = response.sequence {
                        hasher.update(seq.to_string().as_bytes());
                    }
                    format!("{:x}", hasher.finalize())
                };

                if seen_cache.contains(&msg_id) {
                    log::debug!("Skipping duplicate legacy proposal response");
                    continue;
                }

                seen_cache.insert(msg_id);

                // Persist to holder-side cache
                if let Ok(json) = serde_json::to_vec(&response) {
                    if let Err(e) = self
                        .received_proposals_db
                        .insert(response.proposal_id.as_bytes(), json)
                    {
                        log::error!("Failed to cache legacy response: {}", e);
                    }
                }

                log::info!(
                    "Legacy ProposalResponse for proposal: {}",
                    response.proposal_id
                );
                proposal_responses.push((response, dm.from_pubkey.clone()));
                continue;
            }

            // Try to parse as GovernanceBallotDM (legacy)
            if let Ok(ballot) = serde_json::from_str::<GovernanceBallotDM>(&dm.content) {
                let msg_id = {
                    use sha2::{Digest, Sha256};
                    let mut hasher = Sha256::new();
                    hasher.update(b"ballot_dm");
                    hasher.update(ballot.proposal_id.as_bytes());
                    hasher.update(ballot.ballot_timestamp.to_string().as_bytes());
                    if let Some(seq) = ballot.sequence {
                        hasher.update(seq.to_string().as_bytes());
                    }
                    hasher.update(ballot.signed_psbt.as_bytes());
                    format!("{:x}", hasher.finalize())
                };

                if seen_cache.contains(&msg_id) {
                    log::debug!("Skipping duplicate legacy ballot");
                    continue;
                }

                seen_cache.insert(msg_id);
                log::info!("Legacy Ballot for proposal: {}", ballot.proposal_id);
                ballot_dms.push((ballot, dm.from_pubkey.clone()));
                continue;
            }

            // Try to parse as GovernanceBallotReceipt (legacy)
            if let Ok(receipt) = serde_json::from_str::<GovernanceBallotReceipt>(&dm.content) {
                let msg_id = {
                    use sha2::{Digest, Sha256};
                    let mut hasher = Sha256::new();
                    hasher.update(b"ballot_receipt");
                    hasher.update(receipt.proposal_id.as_bytes());
                    hasher.update(receipt.ballot_id.as_bytes());
                    hasher.update(receipt.receipt_timestamp.to_string().as_bytes());
                    format!("{:x}", hasher.finalize())
                };

                if seen_cache.contains(&msg_id) {
                    log::debug!("Skipping duplicate legacy ballot receipt");
                    continue;
                }

                seen_cache.insert(msg_id);
                log::info!("Legacy BallotReceipt for proposal: {}", receipt.proposal_id);
                ballot_receipts.push((receipt, dm.from_pubkey.clone()));
                continue;
            }

            log::debug!("DM from {} is not a governance message", dm.from_pubkey);
        }

        log::info!(
            "Processed governance DMs: {} access requests, {} proposal responses, {} ballots, {} receipts",
            access_requests.len(),
            proposal_responses.len(),
            ballot_dms.len(),
            ballot_receipts.len()
        );

        // Store received ballots in cache so list_ballots can find them
        // This is critical for private governance: ballots arrive via DM, not public Nostr
        if !ballot_dms.is_empty() {
            let mut cache = self.ballot_cache.write().await;
            for (ballot_dm, from_pubkey) in &ballot_dms {
                // Convert GovernanceBallotDM to GovernanceBallot for storage
                let ballot = GovernanceBallot {
                    version: ballot_dm.version,
                    proposal_id: ballot_dm.proposal_id.clone(),
                    asset_id: ballot_dm.asset_id.clone(),
                    voter_nostr_pubkey: Some(from_pubkey.clone()), // Store sender pubkey
                    signed_psbt: ballot_dm.signed_psbt.clone(),
                    ballot_units: ballot_dm.ballot_units,
                    voter_timestamp: ballot_dm.ballot_timestamp,
                    nostr_event_id: None, // Private ballots don't have public event IDs
                };

                cache
                    .entry(ballot_dm.proposal_id.clone())
                    .or_insert_with(Vec::new)
                    .push(ballot);

                log::info!(
                    "Cached private ballot for proposal {} from {} ({} units)",
                    ballot_dm.proposal_id,
                    from_pubkey,
                    ballot_dm.ballot_units
                );
            }
        }

        Ok((
            access_requests,
            proposal_responses,
            ballot_dms,
            ballot_receipts,
        ))
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn test_manager_structure() {
        // Just verify the struct compiles
        // Integration tests will test actual functionality
    }
}
