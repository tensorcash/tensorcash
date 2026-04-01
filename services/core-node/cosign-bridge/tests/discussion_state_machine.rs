// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! State machine and validation tests for discussion transport layer.
//! Complements the inline unit tests in bulletin_board/discussion.rs with
//! cross-module integration scenarios.

use cosign_bridge::bulletin_board::discussion::*;
use cosign_bridge::bulletin_board::governance::OwnershipProof;

fn hash64() -> String {
    "ab".repeat(32)
}

fn hash64_alt() -> String {
    "cd".repeat(32)
}

fn pubkey64() -> String {
    "11".repeat(32)
}

// ============================================================================
// SCOPE VALIDATION
// ============================================================================

#[test]
fn scope_key_model_prealert() {
    let key = build_scope_key(&DiscussionScope::ModelPrealert, &hash64());
    assert_eq!(key, format!("model_prealert:{}", hash64()));
}

#[test]
fn scope_key_model_challenge() {
    let key = build_scope_key(&DiscussionScope::ModelChallenge, &hash64());
    assert_eq!(key, format!("model_challenge:{}", hash64()));
}

#[test]
fn parse_scope_key_roundtrip_both_types() {
    for scope in [
        DiscussionScope::ModelPrealert,
        DiscussionScope::ModelChallenge,
    ] {
        let key = build_scope_key(&scope, &hash64());
        let (parsed_scope, parsed_id) = parse_scope_key(&key).unwrap();
        assert_eq!(parsed_scope, scope);
        assert_eq!(parsed_id, hash64());
    }
}

#[test]
fn parse_scope_key_rejects_unknown_type() {
    let err = parse_scope_key(&format!("general:{}", hash64())).unwrap_err();
    assert!(err.contains("Unsupported"));
}

#[test]
fn parse_scope_key_rejects_no_separator() {
    let err = parse_scope_key("model_prealert_no_colon").unwrap_err();
    assert!(err.contains("scope_type"));
}

#[test]
fn validate_scope_rejects_short_id() {
    let err = validate_scope(&DiscussionScope::ModelPrealert, "deadbeef").unwrap_err();
    assert!(err.contains("64-character"));
}

#[test]
fn validate_scope_rejects_nonhex_id() {
    // 64 chars but 'g' is not hex
    let bad = "g".repeat(64);
    let err = validate_scope(&DiscussionScope::ModelPrealert, &bad).unwrap_err();
    assert!(err.contains("64-character hex"));
}

#[test]
fn validate_scope_accepts_uppercase_hex() {
    let upper = "ABCDEF0123456789".repeat(4);
    assert!(validate_scope(&DiscussionScope::ModelPrealert, &upper).is_ok());
}

// ============================================================================
// POST CONSTRUCTION
// ============================================================================

#[test]
fn post_new_valid() {
    let post = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        "Proposing new model, commits welcome".to_string(),
        None,
        None,
    )
    .unwrap();

    assert_eq!(post.scope_type, DiscussionScope::ModelPrealert);
    assert_eq!(post.scope_id, hash64());
    assert_eq!(post.content, "Proposing new model, commits welcome");
    assert_eq!(post.author_pubkey, pubkey64());
    assert!(post.proof.is_none());
    assert!(post.proof_raw.is_none());
    assert!(post.post_id.is_empty()); // Filled by Nostr publish
}

#[test]
fn post_trims_content() {
    let post = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "tensor".to_string(),
        pubkey64(),
        "  hello world  ".to_string(),
        None,
        None,
    )
    .unwrap();

    assert_eq!(post.content, "hello world");
}

#[test]
fn post_rejects_empty_content() {
    let err = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        "".to_string(),
        None,
        None,
    )
    .unwrap_err();
    assert!(err.contains("cannot be empty"));
}

#[test]
fn post_rejects_whitespace_only() {
    let err = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        "   \n\t  ".to_string(),
        None,
        None,
    )
    .unwrap_err();
    assert!(err.contains("cannot be empty"));
}

#[test]
fn post_rejects_oversized_content() {
    let long = "x".repeat(4097);
    let err = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        long,
        None,
        None,
    )
    .unwrap_err();
    assert!(err.contains("exceeds"));
}

#[test]
fn post_accepts_max_content() {
    let max_content = "x".repeat(4096);
    let post = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        max_content,
        None,
        None,
    );
    assert!(post.is_ok());
}

#[test]
fn post_rejects_bad_scope_id() {
    let err = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        "too_short".to_string(),
        "regtest".to_string(),
        pubkey64(),
        "hello".to_string(),
        None,
        None,
    )
    .unwrap_err();
    assert!(err.contains("64-character"));
}

// ============================================================================
// POST VALIDATION
// ============================================================================

#[test]
fn validate_rejects_bad_author_pubkey() {
    let mut post = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        "hello".to_string(),
        None,
        None,
    )
    .unwrap();

    post.author_pubkey = "short".to_string();
    let err = post.validate().unwrap_err();
    assert!(err.contains("64-character hex"));
}

// ============================================================================
// PROOF ATTACHMENT
// ============================================================================

#[test]
fn post_with_proof_serializes_raw() {
    let proof = OwnershipProof {
        utxo_ref: "abc123:0".to_string(),
        address: "bcrt1qtest".to_string(),
        message: "TENSORCASH_DISCUSS:v1:regtest:model_prealert:test:test:5000".to_string(),
        signature: "sig_hex".to_string(),
        asset_units: 100000,
        asset_id: None,
    };

    let post = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        "with proof".to_string(),
        None,
        Some(proof.clone()),
    )
    .unwrap();

    assert!(post.proof.is_some());
    assert!(post.proof_raw.is_some());
    let raw = post.proof_raw.unwrap();
    assert!(raw.contains("abc123:0"));
    assert!(raw.contains("bcrt1qtest"));
}

#[test]
fn post_without_proof_has_no_raw() {
    let post = DiscussionPost::new(
        DiscussionScope::ModelPrealert,
        hash64(),
        "regtest".to_string(),
        pubkey64(),
        "no proof".to_string(),
        None,
        None,
    )
    .unwrap();

    assert!(post.proof.is_none());
    assert!(post.proof_raw.is_none());
}

// ============================================================================
// SCOPE FILTERING
// ============================================================================

#[test]
fn different_scopes_produce_different_keys() {
    let key_a = build_scope_key(&DiscussionScope::ModelPrealert, &hash64());
    let key_b = build_scope_key(&DiscussionScope::ModelChallenge, &hash64());
    assert_ne!(key_a, key_b);
}

#[test]
fn different_ids_produce_different_keys() {
    let key_a = build_scope_key(&DiscussionScope::ModelPrealert, &hash64());
    let key_b = build_scope_key(&DiscussionScope::ModelPrealert, &hash64_alt());
    assert_ne!(key_a, key_b);
}

// ============================================================================
// SERIALIZATION ROUND-TRIP
// ============================================================================

#[test]
fn post_json_roundtrip() {
    let post = DiscussionPost::new(
        DiscussionScope::ModelChallenge,
        hash64(),
        "tensor-test".to_string(),
        pubkey64(),
        "challenge discussion".to_string(),
        None,
        None,
    )
    .unwrap();

    let json = serde_json::to_string(&post).unwrap();
    let deserialized: DiscussionPost = serde_json::from_str(&json).unwrap();

    assert_eq!(deserialized.scope_type, DiscussionScope::ModelChallenge);
    assert_eq!(deserialized.scope_id, hash64());
    assert_eq!(deserialized.network, "tensor-test");
    assert_eq!(deserialized.content, "challenge discussion");
}

#[test]
fn scope_from_str_roundtrip() {
    for name in ["model_prealert", "model_challenge"] {
        let scope: DiscussionScope = name.parse().unwrap();
        assert_eq!(scope.as_str(), name);
    }
}

#[test]
fn scope_from_str_rejects_invalid() {
    assert!("general".parse::<DiscussionScope>().is_err());
    assert!("".parse::<DiscussionScope>().is_err());
    assert!("MODEL_PREALERT".parse::<DiscussionScope>().is_err()); // case-sensitive
}
