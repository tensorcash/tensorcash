// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Nostr client wrapper for bulletin board

use crate::bulletin_board::discussion::{
    parse_scope_key, DiscussionPost, DISCUSSION_KIND, DISCUSSION_TOPIC,
};
use crate::bulletin_board::governance::OwnershipProof;
use crate::bulletin_board::types::{Offer, OfferFilters};
use crate::stdio::BridgeError;
use nostr_sdk::key::SecretKey;
use nostr_sdk::prelude::{FromBech32, PublicKey};
use nostr_sdk::types::filter::SingleLetterTag;
use nostr_sdk::{Client, EventBuilder, EventId, Filter, Keys, Kind, Tag, Timestamp};
use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Duration;

/// Direct message received from Nostr
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct DirectMessage {
    pub from_pubkey: String,
    pub to_pubkey: String,
    pub content: String,
    pub timestamp: u64,
    pub event_id: String,
}

/// Nostr client for bulletin board operations
///
/// Encryption notes
/// - Outbound DMs are sent via NIP-04 (AES-256-CBC) using nostr-sdk 0.27 helpers.
/// - Inbound DMs are decrypted with the correct peer key (sender for incoming,
///   recipient from the `#p` tag for messages authored by us). This avoids
///   mis-decrypting our own authored DMs with our author key.
/// - If a payload appears to be NIP-44 ("v2:" prefix) we skip decryption with a
///   clear warning rather than emitting a misleading CBC-mode error. A future
///   upgrade to nostr-sdk >= 0.30 should add NIP-44 send/recv.
pub struct NostrClient {
    client: Client,
    keys: Keys,
    relays: Vec<String>,
    use_nip44: bool,
}

impl NostrClient {
    /// Create new Nostr client with key management
    ///
    /// # Arguments
    ///
    /// * `relays` - List of relay URLs (wss://...)
    /// * `nostr_key_path` - Optional path to key file (default: ~/.tensorcash/nostr_keys)
    ///
    /// # Key Management
    ///
    /// - If key file exists: Load keys from file
    /// - If key file doesn't exist: Generate new keypair and save
    /// - File permissions set to 0600 (read/write owner only)
    pub async fn new(
        relays: Vec<String>,
        nostr_key_path: Option<String>,
    ) -> Result<Self, BridgeError> {
        // Determine key file path
        let key_path = if let Some(path) = nostr_key_path {
            PathBuf::from(path)
        } else {
            // Default: ~/.tensorcash/nostr_keys
            let home = std::env::var("HOME")
                .or_else(|_| std::env::var("USERPROFILE"))
                .map_err(|_| {
                    BridgeError::CryptoInitFailed("Cannot determine home directory".to_string())
                })?;
            PathBuf::from(home).join(".tensorcash").join("nostr_keys")
        };

        // Load or generate keys
        let keys = if key_path.exists() {
            log::info!("Loading Nostr keys from {:?}", key_path);
            Self::load_keys(&key_path)?
        } else {
            log::info!("Generating new Nostr keypair, saving to {:?}", key_path);
            let keys = Keys::generate();
            Self::save_keys(&keys, &key_path)?;
            keys
        };

        log::info!("Nostr public key: {}", keys.public_key());

        // Create client
        let client = Client::new(&keys);

        // Connect to relays
        for relay_url in &relays {
            log::info!("Connecting to Nostr relay: {}", relay_url);
            client.add_relay(relay_url.clone()).await.map_err(|e| {
                BridgeError::TransportDown(format!("Failed to add relay {}: {}", relay_url, e))
            })?;
        }

        // Connect to all relays
        client.connect().await;

        // Wait a moment for connections to establish
        tokio::time::sleep(Duration::from_millis(500)).await;

        // Fail fast if the Nostr read path is unavailable. add_relay/connect alone
        // does not prove that any relay is actually reachable and usable.
        client
            .get_events_of(
                vec![Filter::new().limit(1)],
                Some(Duration::from_secs(5)),
            )
            .await
            .map_err(|e| {
                BridgeError::TransportDown(format!(
                    "Failed to query connected Nostr relays after init: {}",
                    e
                ))
            })?;

        Ok(Self {
            client,
            keys,
            relays,
            use_nip44: false, // Default to NIP-04 send for compatibility; receive supports NIP-04/NIP-44
        })
    }

    /// Load keys from file (hex-encoded secret key)
    fn load_keys(path: &PathBuf) -> Result<Keys, BridgeError> {
        let secret_hex = std::fs::read_to_string(path)
            .map_err(|e| BridgeError::CryptoInitFailed(format!("Failed to read keys: {}", e)))?;

        let secret_hex = secret_hex.trim();

        // Decode hex string to bytes
        let secret_bytes = hex::decode(secret_hex)
            .map_err(|e| BridgeError::CryptoInitFailed(format!("Failed to decode hex: {}", e)))?;

        // Parse as SecretKey from bytes
        let secret_key = SecretKey::from_slice(&secret_bytes).map_err(|e| {
            BridgeError::CryptoInitFailed(format!("Failed to parse secret key: {}", e))
        })?;

        Ok(Keys::new(secret_key))
    }

    /// Save keys to file (hex-encoded secret key)
    fn save_keys(keys: &Keys, path: &PathBuf) -> Result<(), BridgeError> {
        // Create directory if needed
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                BridgeError::CryptoInitFailed(format!("Failed to create directory: {}", e))
            })?;
        }

        // Save hex-encoded secret key
        let secret_key = keys.secret_key().unwrap();
        let secret_hex = secret_key.display_secret().to_string();
        std::fs::write(path, secret_hex)
            .map_err(|e| BridgeError::CryptoInitFailed(format!("Failed to write keys: {}", e)))?;

        // Set restrictive permissions (Unix only)
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(path)
                .map_err(|e| {
                    BridgeError::CryptoInitFailed(format!("Failed to get metadata: {}", e))
                })?
                .permissions();
            perms.set_mode(0o600); // rw-------
            std::fs::set_permissions(path, perms).map_err(|e| {
                BridgeError::CryptoInitFailed(format!("Failed to set permissions: {}", e))
            })?;
        }

        Ok(())
    }

    /// Get connected relay URLs
    pub fn get_relays(&self) -> Vec<String> {
        self.relays.clone()
    }

    /// Get public key in hex format
    pub fn get_public_key(&self) -> String {
        self.keys.public_key().to_string()
    }

    /// Send a direct message to another Nostr user
    ///
    /// # Arguments
    ///
    /// * `to_pubkey` - Recipient's public key (npub1... or hex)
    /// * `message` - Message content (will be encrypted)
    ///
    /// # Returns
    ///
    /// Event ID of the sent DM
    pub async fn send_dm(&self, to_pubkey: &str, message: &str) -> Result<String, BridgeError> {
        // Parse recipient public key (support both npub bech32 and hex)
        let to_pubkey: PublicKey = if to_pubkey.starts_with("npub1") {
            PublicKey::from_bech32(to_pubkey)
                .map_err(|e| BridgeError::InvalidCommand(format!("Invalid npub: {}", e)))?
        } else {
            PublicKey::from_hex(to_pubkey)
                .map_err(|e| BridgeError::InvalidCommand(format!("Invalid hex pubkey: {}", e)))?
        };

        if self.use_nip44 {
            // Encrypt with NIP-44 and publish a kind=4 event with #p tag (TagKind::Custom("p"))
            use nostr_sdk::nips::nip44::{self, Version};
            let ciphertext = nip44::encrypt(
                self.keys.secret_key().unwrap(),
                &to_pubkey,
                message,
                Version::V2,
            )
            .map_err(|e| BridgeError::TransportDown(format!("NIP-44 encrypt failed: {}", e)))?;

            // Add recipient using standard 'p' tag via helper
            let tags = vec![Tag::public_key(to_pubkey)];

            let event = EventBuilder::new(Kind::EncryptedDirectMessage, ciphertext, tags)
                .to_event(&self.keys)
                .map_err(|e| {
                    BridgeError::CryptoInitFailed(format!("Failed to build DM event: {}", e))
                })?;

            let event_id = self.client.send_event(event).await.map_err(|e| {
                BridgeError::TransportDown(format!("Failed to send DM (NIP-44): {}", e))
            })?;

            log::info!("Sent DM (NIP-44) to {}: event_id={}", to_pubkey, event_id);
            Ok(event_id.to_string())
        } else {
            // Fallback: NIP-04 direct message
            let event_id = self
                .client
                .send_direct_msg(to_pubkey, message, None)
                .await
                .map_err(|e| BridgeError::TransportDown(format!("Failed to send DM: {}", e)))?;

            log::info!("Sent DM (NIP-04) to {}: event_id={}", to_pubkey, event_id);

            Ok(event_id.to_string())
        }
    }

    /// Fetch direct messages sent to this user
    ///
    /// # Arguments
    ///
    /// * `since` - Optional timestamp to fetch messages since (default: last hour)
    ///
    /// # Returns
    ///
    /// List of decrypted direct messages
    pub async fn fetch_dms(&self, since: Option<u64>) -> Result<Vec<DirectMessage>, BridgeError> {
        let my_pubkey = self.keys.public_key();

        // Default to last hour
        let since_timestamp = since.unwrap_or_else(|| chrono::Utc::now().timestamp() as u64 - 3600);

        // Query for DMs sent TO me (kind 4 with #p tag = my pubkey)
        // plus messages I authored (outgoing trade requests)
        let my_pubkey_hex = my_pubkey.to_string();
        let incoming_filter = Filter::new()
            .kind(Kind::EncryptedDirectMessage)
            .custom_tag(
                SingleLetterTag::lowercase(nostr_sdk::Alphabet::P),
                vec![my_pubkey_hex.clone()],
            )
            .since(Timestamp::from(since_timestamp));

        let outgoing_filter = Filter::new()
            .kind(Kind::EncryptedDirectMessage)
            .author(my_pubkey)
            .since(Timestamp::from(since_timestamp));

        let events = self
            .client
            .get_events_of(
                vec![incoming_filter, outgoing_filter],
                Some(Duration::from_secs(5)),
            )
            .await
            .map_err(|e| BridgeError::TransportDown(format!("Failed to fetch DMs: {}", e)))?;

        let mut messages = Vec::new();
        let mut seen_events: HashSet<String> = HashSet::new();

        for event in events {
            let event_id = event.id.to_string();
            if !seen_events.insert(event_id.clone()) {
                continue;
            }

            // Determine peer pubkey for decryption
            let authored_by_me = event.pubkey == my_pubkey;
            let mut peer_pubkey = event.pubkey;
            // For inbound (authored_by_me == false), rely on the '#p' filter and attempt decrypt directly.
            // For outbound (authored_by_me == true), we must find the recipient from 'p' tag to compute the peer key.
            if authored_by_me {
                if let Some(rec_hex) = event.tags.iter().find_map(|tag| {
                    if let Tag::Generic(kind, values) = tag {
                        if kind == &nostr_sdk::TagKind::Custom("p".into()) && !values.is_empty() {
                            return values.first().cloned();
                        }
                    }
                    None
                }) {
                    if let Ok(pk) = PublicKey::from_hex(rec_hex) {
                        peer_pubkey = pk;
                    }
                }
            }

            // Try NIP-44 first if content appears to be v2, else try NIP-04 first.
            let mut decrypted: Option<String> = None;
            if event.content.starts_with("v2:") {
                if let Ok(content) = nostr_sdk::nips::nip44::decrypt(
                    self.keys.secret_key().unwrap(),
                    &peer_pubkey,
                    &event.content,
                ) {
                    decrypted = Some(content);
                }
            }
            if decrypted.is_none() {
                if let Ok(content) = nostr_sdk::nips::nip04::decrypt(
                    self.keys.secret_key().unwrap(),
                    &peer_pubkey,
                    &event.content,
                ) {
                    decrypted = Some(content);
                } else if !event.content.starts_with("v2:") {
                    // Last resort: if not v2, try NIP-44 anyway (some relays/clients omit prefix)
                    if let Ok(content) = nostr_sdk::nips::nip44::decrypt(
                        self.keys.secret_key().unwrap(),
                        &peer_pubkey,
                        &event.content,
                    ) {
                        decrypted = Some(content);
                    }
                }
            }

            if let Some(content) = decrypted {
                // Extract the #p tag (public key tag) to determine target pubkey for recordkeeping
                let target_pubkey = event
                    .tags
                    .iter()
                    .find_map(|tag| {
                        if let Tag::Generic(kind, values) = tag {
                            if kind == &nostr_sdk::TagKind::Custom("p".into()) && !values.is_empty()
                            {
                                return values.first().cloned();
                            }
                        }
                        None
                    })
                    .unwrap_or_else(|| my_pubkey.to_string());

                messages.push(DirectMessage {
                    from_pubkey: event.pubkey.to_string(),
                    to_pubkey: target_pubkey,
                    content,
                    timestamp: event.created_at.as_u64(),
                    event_id,
                });
            } else {
                // Lower to debug to avoid user-facing warn spam; we already gated by recipient
                log::debug!(
                    "Failed to decrypt applicable DM from {} using peer {}",
                    event.pubkey,
                    peer_pubkey
                );
            }
        }

        log::info!("Fetched {} DMs since {}", messages.len(), since_timestamp);

        Ok(messages)
    }

    /// Publish an offer to Nostr (kind 30078 - application-specific data)
    ///
    /// # Arguments
    ///
    /// * `offer` - Offer to publish
    ///
    /// # Returns
    ///
    /// Event ID of the published offer
    pub async fn publish_offer(&self, offer: &Offer) -> Result<String, BridgeError> {
        // Serialize offer to JSON
        let offer_json = serde_json::to_string(offer).map_err(|e| {
            BridgeError::InvalidCommand(format!("Failed to serialize offer: {}", e))
        })?;

        // Create tags for filtering
        // Use Tag::Generic for application-specific tags with string values
        let mut tags = vec![
            Tag::Generic(
                nostr_sdk::TagKind::Custom("d".into()),
                vec![format!("tensorcash-offer-{}", offer.id)],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("offer_type".into()),
                vec![match offer.offer_type {
                    crate::bulletin_board::types::OfferType::Buy => "buy".to_string(),
                    crate::bulletin_board::types::OfferType::Sell => "sell".to_string(),
                    crate::bulletin_board::types::OfferType::Swap => "swap".to_string(),
                    crate::bulletin_board::types::OfferType::RepoContract => {
                        "repo_contract".to_string()
                    }
                    crate::bulletin_board::types::OfferType::ForwardContract => {
                        "forward_contract".to_string()
                    }
                    crate::bulletin_board::types::OfferType::SpotContract => {
                        "spot_contract".to_string()
                    }
                    crate::bulletin_board::types::OfferType::DifficultyContract => {
                        "difficulty_contract".to_string()
                    }
                }],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("asset_send".into()),
                vec![offer.asset_send.clone()],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("asset_recv".into()),
                vec![offer.asset_recv.clone()],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("amount".into()),
                vec![offer.amount.to_string()],
            ),
            // Network tag for chain compartmentalization (main, signet, testnet3, regtest)
            Tag::Generic(
                nostr_sdk::TagKind::Custom("network".into()),
                vec![offer.network.clone()],
            ),
            Tag::Expiration(Timestamp::from(offer.expires_at)),
        ];

        // Add contract-specific tags if this is a contract offer
        if let Some(ref contract_type) = offer.contract_type {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("contract_type".into()),
                vec![match contract_type {
                    crate::bulletin_board::types::ContractType::Repo => "repo".to_string(),
                    crate::bulletin_board::types::ContractType::Forward => "forward".to_string(),
                    crate::bulletin_board::types::ContractType::Spot => "spot".to_string(),
                    crate::bulletin_board::types::ContractType::Difficulty => "difficulty".to_string(),
                }],
            ));
        }

        if let Some(ref maker_role) = offer.maker_role {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("maker_role".into()),
                vec![maker_role.clone()],
            ));
        }

        if let Some(apr) = offer.apr {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("apr".into()),
                vec![apr.to_string()],
            ));
        }

        if let Some(ltv) = offer.ltv {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("ltv".into()),
                vec![ltv.to_string()],
            ));
        }

        if let Some(tenor_days) = offer.tenor_days {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("tenor_days".into()),
                vec![tenor_days.to_string()],
            ));
        }

        // Add optional tags
        for region in &offer.regions {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("region".into()),
                vec![region.clone()],
            ));
        }

        for payment_method in &offer.payment_methods {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("payment_method".into()),
                vec![payment_method.clone()],
            ));
        }

        // Build event (kind 30078 - replaceable parameterized event)
        let event = EventBuilder::new(Kind::Custom(30078), offer_json, tags)
            .to_event(&self.keys)
            .map_err(|e| BridgeError::CryptoInitFailed(format!("Failed to build event: {}", e)))?;

        // Publish to all relays
        let event_id =
            self.client.send_event(event).await.map_err(|e| {
                BridgeError::TransportDown(format!("Failed to publish offer: {}", e))
            })?;

        log::info!(
            "Published offer {} to Nostr: event_id={}",
            offer.id,
            event_id
        );

        Ok(event_id.to_string())
    }

    /// Query offers from Nostr with filters
    ///
    /// # Arguments
    ///
    /// * `filters` - Filters to apply (offer_type, region, payment_method, etc.)
    ///
    /// # Returns
    ///
    /// List of offers matching filters
    pub async fn query_offers(&self, filters: OfferFilters) -> Result<Vec<Offer>, BridgeError> {
        // Build Nostr filter
        let filter = Filter::new().kind(Kind::Custom(30078));

        // Note: nostr-sdk 0.27 doesn't support custom_tag in Filter builder
        // We'll filter client-side after fetching

        // Query events
        let events = self
            .client
            .get_events_of(vec![filter], Some(Duration::from_secs(10)))
            .await
            .map_err(|e| BridgeError::TransportDown(format!("Failed to query offers: {}", e)))?;

        let mut offers = Vec::new();

        for event in events {
            // Deserialize offer from event content
            match serde_json::from_str::<Offer>(&event.content) {
                Ok(mut offer) => {
                    // Store Nostr event ID
                    offer.nostr_event_id = Some(event.id.to_string());

                    // Apply client-side filters
                    // Network filter is the most important - compartmentalizes by chain
                    if let Some(ref network_filter) = filters.network {
                        if &offer.network != network_filter {
                            continue;
                        }
                    }

                    if let Some(ref offer_type_filter) = filters.offer_type {
                        let matches = match offer.offer_type {
                            crate::bulletin_board::types::OfferType::Buy => {
                                offer_type_filter == "buy"
                            }
                            crate::bulletin_board::types::OfferType::Sell => {
                                offer_type_filter == "sell"
                            }
                            crate::bulletin_board::types::OfferType::Swap => {
                                offer_type_filter == "swap"
                            }
                            crate::bulletin_board::types::OfferType::RepoContract => {
                                offer_type_filter == "repo_contract"
                            }
                            crate::bulletin_board::types::OfferType::ForwardContract => {
                                offer_type_filter == "forward_contract"
                            }
                            crate::bulletin_board::types::OfferType::SpotContract => {
                                offer_type_filter == "spot_contract"
                            }
                            crate::bulletin_board::types::OfferType::DifficultyContract => {
                                offer_type_filter == "difficulty_contract"
                            }
                        };
                        if !matches {
                            continue;
                        }
                    }

                    if let Some(ref region_filter) = filters.region {
                        if !offer.regions.iter().any(|r| r == region_filter) {
                            continue;
                        }
                    }

                    if let Some(ref payment_filter) = filters.payment_method {
                        if !offer.payment_methods.iter().any(|p| p == payment_filter) {
                            continue;
                        }
                    }

                    if let Some(min_amount) = filters.min_amount {
                        if offer.amount < min_amount {
                            continue;
                        }
                    }

                    if let Some(max_amount) = filters.max_amount {
                        if offer.amount > max_amount {
                            continue;
                        }
                    }

                    // Skip expired offers
                    if offer.is_expired() {
                        continue;
                    }

                    offers.push(offer);
                }
                Err(e) => {
                    log::warn!("Failed to parse offer from event {}: {}", event.id, e);
                }
            }
        }

        log::info!("Queried {} offers from Nostr", offers.len());

        Ok(offers)
    }

    /// Delete an offer from Nostr
    ///
    /// # Arguments
    ///
    /// * `event_id` - Event ID of the offer to delete
    ///
    /// # Note
    ///
    /// This publishes a deletion event (kind 5). Relays may or may not honor it.
    pub async fn delete_offer(&self, event_id: &str) -> Result<(), BridgeError> {
        // Parse event ID
        let event_id = EventId::from_hex(event_id)
            .map_err(|e| BridgeError::InvalidCommand(format!("Invalid event ID: {}", e)))?;

        // Create deletion event (kind 5)
        let deletion = EventBuilder::delete(vec![event_id])
            .to_event(&self.keys)
            .map_err(|e| {
                BridgeError::CryptoInitFailed(format!("Failed to build deletion: {}", e))
            })?;

        // Publish deletion
        self.client
            .send_event(deletion)
            .await
            .map_err(|e| BridgeError::TransportDown(format!("Failed to delete offer: {}", e)))?;

        log::info!("Deleted offer: event_id={}", event_id);

        Ok(())
    }

    /// Publish a governance proposal as a Nostr note (kind 1)
    ///
    /// # Arguments
    ///
    /// * `proposal_json` - JSON-serialized governance proposal
    ///
    /// # Returns
    ///
    /// Event ID of the published note
    pub async fn publish_governance_note(
        &self,
        proposal_json: &str,
    ) -> Result<String, BridgeError> {
        // Create event builder with governance tag using Alphabet::T for consistency with query
        let tags = vec![Tag::Generic(
            nostr_sdk::TagKind::Custom("t".into()),
            vec!["tensorcash_governance".to_string()],
        )];

        let event = EventBuilder::new(Kind::TextNote, proposal_json, tags)
            .to_event(&self.keys)
            .map_err(|e| {
                BridgeError::CryptoInitFailed(format!("Failed to build governance note: {}", e))
            })?;

        // Publish to all relays
        let event_id = self.client.send_event(event).await.map_err(|e| {
            BridgeError::TransportDown(format!("Failed to publish governance note: {}", e))
        })?;

        log::info!("Published governance note: event_id={}", event_id);

        Ok(event_id.to_string())
    }

    /// Publish a governance ballot (holder vote) to Nostr
    ///
    /// # Arguments
    ///
    /// * `ballot_json` - JSON-serialized GovernanceBallot
    ///
    /// # Returns
    ///
    /// Nostr event ID
    pub async fn publish_governance_ballot_note(
        &self,
        ballot_json: &str,
    ) -> Result<String, BridgeError> {
        // Create event builder with ballot tag
        let tags = vec![Tag::Generic(
            nostr_sdk::TagKind::Custom("t".into()),
            vec!["tensorcash_ballot".to_string()],
        )];

        let event = EventBuilder::new(Kind::TextNote, ballot_json, tags)
            .to_event(&self.keys)
            .map_err(|e| {
                BridgeError::CryptoInitFailed(format!("Failed to build ballot note: {}", e))
            })?;

        // Publish to all relays
        let event_id = self.client.send_event(event).await.map_err(|e| {
            BridgeError::TransportDown(format!("Failed to publish ballot note: {}", e))
        })?;

        log::info!("Published ballot note: event_id={}", event_id);

        Ok(event_id.to_string())
    }

    /// Publish a discussion post as a regular append-only Nostr event.
    pub async fn publish_discussion_post(
        &self,
        post: &DiscussionPost,
    ) -> Result<String, BridgeError> {
        let mut tags = vec![
            Tag::Generic(
                nostr_sdk::TagKind::Custom("t".into()),
                vec![DISCUSSION_TOPIC.to_string()],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("d".into()),
                vec![post.scope_key()],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("network".into()),
                vec![post.network.clone()],
            ),
        ];

        let proof_raw = if let Some(raw) = &post.proof_raw {
            Some(raw.clone())
        } else {
            post.proof
                .as_ref()
                .map(serde_json::to_string)
                .transpose()
                .map_err(|e| {
                    BridgeError::InvalidCommand(format!(
                        "Failed to serialize discussion proof: {}",
                        e
                    ))
                })?
        };

        if let Some(raw) = proof_raw {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("proof".into()),
                vec![raw],
            ));
        }

        if let Some(identifier) = &post.model_identifier {
            tags.push(Tag::Generic(
                nostr_sdk::TagKind::Custom("identifier".into()),
                vec![identifier.clone()],
            ));
        }

        let event = EventBuilder::new(Kind::Custom(DISCUSSION_KIND), post.content.clone(), tags)
            .to_event(&self.keys)
            .map_err(|e| {
                BridgeError::CryptoInitFailed(format!("Failed to build discussion event: {}", e))
            })?;

        let event_id = self.client.send_event(event).await.map_err(|e| {
            BridgeError::TransportDown(format!("Failed to publish discussion post: {}", e))
        })?;

        log::info!(
            "Published discussion post for scope {}: event_id={}",
            post.scope_key(),
            event_id
        );

        Ok(event_id.to_string())
    }

    /// Query model discussion posts from Nostr relays.
    pub async fn query_discussion_posts(
        &self,
        scope_key: Option<&str>,
        network: Option<&str>,
        since: Option<u64>,
    ) -> Result<Vec<DiscussionPost>, BridgeError> {
        let mut filter = Filter::new()
            .kind(Kind::Custom(DISCUSSION_KIND))
            .custom_tag(
                SingleLetterTag::lowercase(nostr_sdk::Alphabet::T),
                vec![DISCUSSION_TOPIC.to_string()],
            )
            .since(Timestamp::from(since.unwrap_or_else(|| {
                chrono::Utc::now().timestamp().max(0) as u64 - (86400 * 30)
            })));

        if let Some(scope_key) = scope_key {
            filter = filter.custom_tag(
                SingleLetterTag::lowercase(nostr_sdk::Alphabet::D),
                vec![scope_key.to_string()],
            );
        }

        let events = self
            .client
            .get_events_of(vec![filter], Some(Duration::from_secs(10)))
            .await
            .map_err(|e| {
                BridgeError::TransportDown(format!("Failed to query discussion posts: {}", e))
            })?;

        let mut posts = Vec::new();

        for event in events {
            if !tag_contains_value(&event.tags, DISCUSSION_TOPIC) {
                continue;
            }

            let Some(event_scope_key) = first_tag_value(&event.tags, "d") else {
                log::warn!("Skipping discussion event {} without d tag", event.id);
                continue;
            };

            let (scope_type, scope_id) = match parse_scope_key(&event_scope_key) {
                Ok(parsed) => parsed,
                Err(e) => {
                    log::warn!(
                        "Skipping discussion event {} with invalid d tag: {}",
                        event.id,
                        e
                    );
                    continue;
                }
            };

            let Some(event_network) = first_tag_value(&event.tags, "network") else {
                log::warn!("Skipping discussion event {} without network tag", event.id);
                continue;
            };

            if let Some(expected_network) = network {
                if event_network != expected_network {
                    continue;
                }
            }

            let proof_raw = first_tag_value(&event.tags, "proof");
            let model_identifier = first_tag_value(&event.tags, "identifier");
            let (proof, proof_parse_error) = match &proof_raw {
                Some(raw) => match serde_json::from_str::<OwnershipProof>(raw) {
                    Ok(proof) => (Some(proof), None),
                    Err(e) => (None, Some(e.to_string())),
                },
                None => (None, None),
            };

            posts.push(DiscussionPost {
                post_id: event.id.to_string(),
                scope_type,
                scope_id,
                network: event_network,
                author_pubkey: event.pubkey.to_string(),
                content: event.content.clone(),
                model_identifier,
                created_at: event.created_at.as_u64(),
                proof,
                proof_raw,
                proof_parse_error,
            });
        }

        posts.sort_by(|a, b| a.created_at.cmp(&b.created_at));

        log::info!("Queried {} discussion posts from Nostr", posts.len());

        Ok(posts)
    }

    /// Query governance proposals from Nostr
    ///
    /// # Returns
    ///
    /// List of governance proposals
    pub async fn query_governance_notes(
        &self,
    ) -> Result<Vec<crate::bulletin_board::governance::GovernanceProposal>, BridgeError> {
        use crate::bulletin_board::governance::GovernanceProposal;

        // Query for text notes (filter tags client-side for "t" = tensorcash_governance)
        let filter = Filter::new().kind(Kind::TextNote).since(Timestamp::from(
            chrono::Utc::now().timestamp() as u64 - (86400 * 30),
        )); // Last 30 days

        let events = self
            .client
            .get_events_of(vec![filter], Some(Duration::from_secs(10)))
            .await
            .map_err(|e| {
                BridgeError::TransportDown(format!("Failed to query governance notes: {}", e))
            })?;

        let mut proposals = Vec::new();

        for event in events {
            // Only accept events that have a governance tag.
            // nostr-sdk 0.30+ represents ["t","..."] as Tag::Hashtag, while older
            // versions or other publishers may use Tag::Generic with arbitrary kind.
            let has_tag = event.tags.iter().any(|tag| match tag {
                Tag::Hashtag(value) => value == "tensorcash_governance",
                Tag::Generic(_, values) => values.iter().any(|v| v == "tensorcash_governance"),
                _ => false,
            });
            if !has_tag {
                continue;
            }

            // Parse JSON content
            match serde_json::from_str::<GovernanceProposal>(&event.content) {
                Ok(proposal) => {
                    proposals.push(proposal);
                }
                Err(e) => {
                    log::warn!(
                        "Failed to parse governance proposal from event {}: {}",
                        event.id,
                        e
                    );
                }
            }
        }

        log::info!(
            "Queried {} governance proposals from Nostr",
            proposals.len()
        );

        Ok(proposals)
    }
}

fn first_tag_value(tags: &[Tag], name: &str) -> Option<String> {
    tags.iter().find_map(|tag| {
        match tag {
            Tag::Hashtag(value) if name == "t" => return Some(value.to_string()),
            Tag::Generic(kind, values)
                if kind == &nostr_sdk::TagKind::Custom(name.into()) && !values.is_empty() =>
            {
                return values.first().cloned();
            }
            _ => {}
        }

        // Fallback for sdk representations that don't map to Tag::Generic directly.
        let raw = tag.clone().to_vec();
        if raw.len() >= 2 && raw[0] == name {
            Some(raw[1].to_string())
        } else {
            None
        }
    })
}

fn tag_contains_value(tags: &[Tag], expected: &str) -> bool {
    tags.iter().any(|tag| {
        match tag {
            Tag::Hashtag(value) => {
                if value == expected {
                    return true;
                }
            }
            Tag::Generic(_, values) => {
                if values.iter().any(|value| value == expected) {
                    return true;
                }
            }
            _ => {}
        }

        // Fallback for sdk representations that don't map to Tag::Generic directly.
        tag.clone()
            .to_vec()
            .iter()
            .skip(1)
            .any(|value| value == expected)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_key_generation_and_loading() {
        let temp_dir = TempDir::new().unwrap();
        let key_path = temp_dir.path().join("nostr_keys");

        // Generate keys
        let keys = Keys::generate();
        NostrClient::save_keys(&keys, &key_path).unwrap();

        // Verify file exists
        assert!(key_path.exists());

        // Load keys
        let loaded_keys = NostrClient::load_keys(&key_path).unwrap();

        // Verify keys match
        assert_eq!(keys.public_key(), loaded_keys.public_key());
    }

    #[test]
    #[cfg(unix)]
    fn test_key_file_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let temp_dir = TempDir::new().unwrap();
        let key_path = temp_dir.path().join("nostr_keys");

        let keys = Keys::generate();
        NostrClient::save_keys(&keys, &key_path).unwrap();

        // Check permissions are 0600
        let metadata = std::fs::metadata(&key_path).unwrap();
        let permissions = metadata.permissions();
        assert_eq!(permissions.mode() & 0o777, 0o600);
    }

    #[test]
    fn test_direct_message_structure() {
        let dm = DirectMessage {
            from_pubkey: "npub1test".to_string(),
            to_pubkey: "npub1me".to_string(),
            content: "Hello".to_string(),
            timestamp: 123456,
            event_id: "event123".to_string(),
        };

        assert_eq!(dm.content, "Hello");
        assert_eq!(dm.timestamp, 123456);
    }

    #[test]
fn test_discussion_tag_helpers() {
        let tags = vec![
            Tag::Generic(
                nostr_sdk::TagKind::Custom("d".into()),
                vec!["model_prealert:ab".repeat(16)],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("t".into()),
                vec![DISCUSSION_TOPIC.to_string()],
            ),
            Tag::Generic(
                nostr_sdk::TagKind::Custom("network".into()),
                vec!["regtest".to_string()],
            ),
        ];

        assert!(tag_contains_value(&tags, DISCUSSION_TOPIC));
        assert_eq!(first_tag_value(&tags, "network").unwrap(), "regtest");
    }
}
