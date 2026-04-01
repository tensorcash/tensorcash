// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Schema dispatch for cross-chain payloads inside SpotContract offers.
//!
//! When a bulletin board offer has `contract_type == Spot`, the taker
//! and Qt dispatcher inspect `contract_payload` for the `schema` field.
//! If `schema == "cross_chain_spot_v1"`, the offer is routed to the
//! cross-chain execution path instead of the normal spot path.
//!
//! This keeps the public board footprint stable while giving the taker
//! enough information to branch without adding a new top-level board field.

use super::types::{CrossChainSpotV1Payload, CROSS_CHAIN_SPOT_V1_SCHEMA};
use crate::bulletin_board::types::{ContractType, Offer};

/// Check whether an offer's contract payload contains a cross-chain schema.
///
/// Returns `true` if the offer is a `SpotContract` whose payload's `schema`
/// field equals `"cross_chain_spot_v1"`.
///
/// This function is intentionally cheap — it only parses enough JSON to
/// read the `schema` field, not the full payload.
pub fn is_cross_chain_payload(offer: &Offer) -> bool {
    // Must be a SpotContract
    if offer.contract_type.as_ref() != Some(&ContractType::Spot) {
        return false;
    }

    let payload_str = match &offer.contract_payload {
        Some(s) => s,
        None => return false,
    };

    // Try to decode as base64 first, fall back to raw JSON
    let json_str = match base64_decode_to_string(payload_str) {
        Some(decoded) => decoded,
        None => payload_str.clone(),
    };

    // Parse just enough to check the schema field
    match serde_json::from_str::<serde_json::Value>(&json_str) {
        Ok(val) => val.get("schema").and_then(|s| s.as_str()) == Some(CROSS_CHAIN_SPOT_V1_SCHEMA),
        Err(_) => false,
    }
}

/// Extract and validate a full cross-chain payload from an offer.
///
/// Returns `Ok(payload)` if the offer contains a valid cross-chain payload,
/// or `Err(reason)` if parsing or validation fails.
pub fn extract_cross_chain_payload(offer: &Offer) -> Result<CrossChainSpotV1Payload, String> {
    // Must be a SpotContract
    if offer.contract_type.as_ref() != Some(&ContractType::Spot) {
        return Err("offer is not a SpotContract".to_string());
    }

    let payload_str = offer
        .contract_payload
        .as_ref()
        .ok_or_else(|| "offer has no contract_payload".to_string())?;

    // Decode base64 or use raw JSON
    let json_str = match base64_decode_to_string(payload_str) {
        Some(decoded) => decoded,
        None => payload_str.clone(),
    };

    // Parse full payload
    let payload: CrossChainSpotV1Payload = serde_json::from_str(&json_str)
        .map_err(|e| format!("failed to parse cross-chain payload: {}", e))?;

    // Validate internal consistency
    payload.validate()?;

    Ok(payload)
}

/// Try to decode a string as base64 into a UTF-8 string.
/// Returns `None` if decoding fails (input is probably already raw JSON).
fn base64_decode_to_string(s: &str) -> Option<String> {
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD.decode(s).ok()?;
    String::from_utf8(bytes).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bulletin_board::types::{ContractType, Offer};

    fn make_spot_offer(payload_json: &str) -> Offer {
        Offer::new_contract(
            ContractType::Spot,
            payload_json.to_string(),
            "maker".to_string(),
            "npub1test".to_string(),
            "regtest".to_string(),
            None,
            None,
            None,
        )
    }

    fn sample_cross_chain_json() -> String {
        serde_json::json!({
            "schema": "cross_chain_spot_v1",
            "id": "test-uuid",
            "role": "maker",
            "tsc_leg": {
                "asset_id": "native",
                "units": "100000000"
            },
            "external_leg": {
                "chain": "btc",
                "asset": "BTC",
                "units": "100000000",
                "settlement_address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
                "refund_address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
                "adapter": "btc_scriptless_v1"
            },
            "funding_order": "tsc_first",
            "confirmation_policy": {
                "external_min_conf": 6,
                "tsc_min_conf": 1,
                "reorg_conf": 6
            },
            "timeout_policy": {
                "external_lock_seconds": 86400,
                "tsc_lock_blocks": 288,
                "claim_budget_seconds": 21600,
                "reorg_margin_seconds": 3600,
                "min_timeout_gap_seconds": 25200
            },
            "fee_policy": {
                "claim_strategy": "rbf_or_cpfp",
                "refund_strategy": "rbf_or_cpfp",
                "fee_funding_mode": "reserved_utxo"
            }
        })
        .to_string()
    }

    #[test]
    fn test_is_cross_chain_raw_json() {
        let offer = make_spot_offer(&sample_cross_chain_json());
        assert!(is_cross_chain_payload(&offer));
    }

    #[test]
    fn test_is_cross_chain_base64() {
        use base64::Engine;
        let json = sample_cross_chain_json();
        let b64 = base64::engine::general_purpose::STANDARD.encode(json.as_bytes());
        let offer = make_spot_offer(&b64);
        assert!(is_cross_chain_payload(&offer));
    }

    #[test]
    fn test_not_cross_chain_normal_spot() {
        let offer = make_spot_offer(r#"{"schema": "spot_v1", "some_field": "value"}"#);
        assert!(!is_cross_chain_payload(&offer));
    }

    #[test]
    fn test_not_cross_chain_repo() {
        let offer = Offer::new_contract(
            ContractType::Repo,
            sample_cross_chain_json(),
            "lender".to_string(),
            "npub1test".to_string(),
            "regtest".to_string(),
            None,
            None,
            None,
        );
        assert!(!is_cross_chain_payload(&offer));
    }

    #[test]
    fn test_extract_valid() {
        let offer = make_spot_offer(&sample_cross_chain_json());
        let payload = extract_cross_chain_payload(&offer).unwrap();
        assert_eq!(payload.schema, "cross_chain_spot_v1");
        assert_eq!(
            payload.external_leg.chain,
            super::super::types::ExternalChain::Btc
        );
    }

    #[test]
    fn test_extract_invalid_schema() {
        let mut json: serde_json::Value = serde_json::from_str(&sample_cross_chain_json()).unwrap();
        json["schema"] = serde_json::json!("wrong");
        let offer = make_spot_offer(&json.to_string());
        assert!(extract_cross_chain_payload(&offer).is_err());
    }

    #[test]
    fn test_no_payload() {
        let mut offer = make_spot_offer("{}");
        offer.contract_payload = None;
        assert!(!is_cross_chain_payload(&offer));
    }
}
