// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Public discussion transport types for model-scoped pre-alert and challenge threads.

use crate::bulletin_board::governance::OwnershipProof;
use serde::{Deserialize, Serialize};
use std::str::FromStr;

pub const DISCUSSION_KIND: u64 = 8322;
pub const DISCUSSION_TOPIC: &str = "tensorcash_discuss";
const MAX_DISCUSSION_CONTENT_LEN: usize = 4096;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum DiscussionScope {
    ModelPrealert,
    ModelChallenge,
}

impl DiscussionScope {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::ModelPrealert => "model_prealert",
            Self::ModelChallenge => "model_challenge",
        }
    }
}

impl FromStr for DiscussionScope {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "model_prealert" => Ok(Self::ModelPrealert),
            "model_challenge" => Ok(Self::ModelChallenge),
            _ => Err(format!("Unsupported discussion scope_type: {}", value)),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscussionPost {
    /// Nostr event ID for the published post.
    pub post_id: String,

    /// Scope of the discussion thread.
    pub scope_type: DiscussionScope,

    /// 32-byte hash identifier encoded as 64 hex chars.
    pub scope_id: String,

    /// Network compartment the post belongs to.
    pub network: String,

    /// Nostr pubkey that authored the post.
    pub author_pubkey: String,

    /// User-visible message body.
    pub content: String,

    /// Optional human-readable identifier for model pre-alert threads.
    /// Format: model_name@commit_id
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_identifier: Option<String>,

    /// Event timestamp.
    pub created_at: u64,

    /// Parsed proof payload when available and valid JSON.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proof: Option<OwnershipProof>,

    /// Raw proof JSON carried in the Nostr tag, preserved for bcore verification.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proof_raw: Option<String>,

    /// Parse failure for malformed proof tags.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proof_parse_error: Option<String>,
}

impl DiscussionPost {
    pub fn new(
        scope_type: DiscussionScope,
        scope_id: String,
        network: String,
        author_pubkey: String,
        content: String,
        model_identifier: Option<String>,
        proof: Option<OwnershipProof>,
    ) -> Result<Self, String> {
        validate_scope(&scope_type, &scope_id)?;

        let trimmed = content.trim();
        if trimmed.is_empty() {
            return Err("Discussion content cannot be empty".to_string());
        }
        if trimmed.len() > MAX_DISCUSSION_CONTENT_LEN {
            return Err(format!(
                "Discussion content exceeds {} characters",
                MAX_DISCUSSION_CONTENT_LEN
            ));
        }

        let proof_raw = proof
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .map_err(|e| format!("Failed to serialize proof: {}", e))?;

        Ok(Self {
            post_id: String::new(),
            scope_type,
            scope_id,
            network,
            author_pubkey,
            content: trimmed.to_string(),
            model_identifier: normalize_model_identifier(model_identifier)?,
            created_at: chrono::Utc::now().timestamp().max(0) as u64,
            proof,
            proof_raw,
            proof_parse_error: None,
        })
    }

    pub fn scope_key(&self) -> String {
        build_scope_key(&self.scope_type, &self.scope_id)
    }

    pub fn validate(&self) -> Result<(), String> {
        validate_scope(&self.scope_type, &self.scope_id)?;

        if self.content.trim().is_empty() {
            return Err("Discussion content cannot be empty".to_string());
        }
        if self.content.len() > MAX_DISCUSSION_CONTENT_LEN {
            return Err(format!(
                "Discussion content exceeds {} characters",
                MAX_DISCUSSION_CONTENT_LEN
            ));
        }
        normalize_model_identifier(self.model_identifier.clone())?;
        if self.author_pubkey.len() != 64
            || !self.author_pubkey.chars().all(|c| c.is_ascii_hexdigit())
        {
            return Err("author_pubkey must be a 64-character hex pubkey".to_string());
        }

        Ok(())
    }
}

fn normalize_model_identifier(value: Option<String>) -> Result<Option<String>, String> {
    let Some(value) = value else {
        return Ok(None);
    };

    let trimmed = value.trim().to_string();
    if trimmed.is_empty() {
        return Ok(None);
    }
    if trimmed.len() > 512 {
        return Err("model_identifier exceeds 512 characters".to_string());
    }
    if !trimmed.contains('@') {
        return Err("model_identifier must be in model_name@commit_id format".to_string());
    }
    Ok(Some(trimmed))
}

pub fn build_scope_key(scope_type: &DiscussionScope, scope_id: &str) -> String {
    format!("{}:{}", scope_type.as_str(), scope_id)
}

pub fn parse_scope_key(scope_key: &str) -> Result<(DiscussionScope, String), String> {
    let (scope_type_str, scope_id) = scope_key
        .split_once(':')
        .ok_or_else(|| "Discussion scope key must be <scope_type>:<scope_id>".to_string())?;

    let scope_type = DiscussionScope::from_str(scope_type_str)?;
    validate_scope(&scope_type, scope_id)?;

    Ok((scope_type, scope_id.to_string()))
}

pub fn validate_scope(scope_type: &DiscussionScope, scope_id: &str) -> Result<(), String> {
    match scope_type {
        DiscussionScope::ModelPrealert | DiscussionScope::ModelChallenge => {}
    }

    if !is_hex_hash(scope_id) {
        return Err("scope_id must be a 64-character hex hash".to_string());
    }

    Ok(())
}

fn is_hex_hash(value: &str) -> bool {
    value.len() == 64 && value.chars().all(|c| c.is_ascii_hexdigit())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hash64() -> String {
        "ab".repeat(32)
    }

    fn pubkey64() -> String {
        "cd".repeat(32)
    }

    #[test]
    fn discussion_scope_roundtrip() {
        let scope_id = hash64();
        let scope_key = build_scope_key(&DiscussionScope::ModelPrealert, &scope_id);
        let (scope_type, parsed_scope_id) = parse_scope_key(&scope_key).unwrap();

        assert_eq!(scope_type, DiscussionScope::ModelPrealert);
        assert_eq!(parsed_scope_id, scope_id);
    }

    #[test]
    fn discussion_scope_rejects_short_hash() {
        let err = parse_scope_key("model_prealert:deadbeef").unwrap_err();
        assert!(err.contains("64-character hex"));
    }

    #[test]
    fn discussion_post_serializes_proof_raw() {
        let proof = OwnershipProof {
            utxo_ref: "txid:0".to_string(),
            address: "bcrt1qtest".to_string(),
            message: "TENSORCASH_DISCUSS:v1:regtest:model_prealert:test:test:100".to_string(),
            signature: "signature".to_string(),
            asset_units: 42,
            asset_id: None,
        };

        let post = DiscussionPost::new(
            DiscussionScope::ModelPrealert,
            hash64(),
            "regtest".to_string(),
            pubkey64(),
            "pre-alert".to_string(),
            Some("model@commit".to_string()),
            Some(proof),
        )
        .unwrap();

        assert!(post.proof_raw.is_some());
        assert!(post.proof_parse_error.is_none());
        assert_eq!(post.scope_key(), format!("model_prealert:{}", hash64()));
    }

    #[test]
    fn discussion_post_rejects_empty_content() {
        let err = DiscussionPost::new(
            DiscussionScope::ModelChallenge,
            hash64(),
            "regtest".to_string(),
            pubkey64(),
            "   ".to_string(),
            None,
            None,
        )
        .unwrap_err();

        assert!(err.contains("cannot be empty"));
    }
}
