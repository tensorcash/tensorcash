// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Cross-chain spot v1 payload schema and supporting types.
//!
//! These types define the contract payload that rides inside the existing
//! `Offer.contract_payload` field when `contract_type == Spot` and the
//! schema field is `"cross_chain_spot_v1"`.

use serde::{Deserialize, Serialize};

use super::validation::validate_external_address;

/// Schema identifier for cross-chain spot v1 payloads.
pub const CROSS_CHAIN_SPOT_V1_SCHEMA: &str = "cross_chain_spot_v1";

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

/// Supported external chains.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ExternalChain {
    Btc,
    Ethereum,
    Tron,
}

impl std::fmt::Display for ExternalChain {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Btc => write!(f, "btc"),
            Self::Ethereum => write!(f, "ethereum"),
            Self::Tron => write!(f, "tron"),
        }
    }
}

/// Adapter implementation for the external leg.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum AdapterKind {
    #[serde(rename = "btc_scriptless_v1")]
    BtcScriptlessV1,
    #[serde(rename = "eth_htlc_v1")]
    EthHtlcV1,
    #[serde(rename = "tron_htlc_v1")]
    TronHtlcV1,
}

/// Which side funds first.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FundingOrder {
    TscFirst,
    ExternalFirst,
}

/// How fee-bump funding is sourced.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeeFundingMode {
    /// UTXO-based reserved fee input (BTC/TSC).
    ReservedUtxo,
    /// Account-balance gas reservation (ETH/TRON).
    ReservedBalance,
}

/// Claim/refund fee strategy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeeStrategy {
    RbfOrCpfp,
    GasEscalator,
}

/// Fee speed preference for settlement profiles.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FeeSpeed {
    Normal,
    Fast,
    Urgent,
}

// ---------------------------------------------------------------------------
// Payload structs
// ---------------------------------------------------------------------------

/// The TSC-side leg of a cross-chain swap.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TscLeg {
    /// Asset identifier (hex for issued assets, or empty/`"native"` for native TSC).
    pub asset_id: String,

    /// Amount as a decimal string to avoid cross-language precision bugs.
    pub units: String,
}

/// The external-chain leg of a cross-chain swap.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExternalLeg {
    /// Which external chain.
    pub chain: ExternalChain,

    /// Asset ticker (e.g. `"BTC"`, `"ETH"`, `"USDT"`).
    pub asset: String,

    /// Amount as a decimal string.
    pub units: String,

    /// Final settlement address on the external chain.
    pub settlement_address: String,

    /// Refund address on the external chain (may equal settlement_address).
    pub refund_address: String,

    /// Adapter implementation to use.
    pub adapter: AdapterKind,
}

/// Confirmation thresholds for both chains.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfirmationPolicy {
    /// Minimum confirmations required on the external chain before advancing.
    pub external_min_conf: u32,

    /// Minimum confirmations required on TSC before advancing.
    pub tsc_min_conf: u32,

    /// Confirmations required to consider a tx safe against reorgs.
    /// Named `reorg_conf` in the payload but distinct from
    /// `ForwardTerms::reorg_conf` which is TSC-only.
    pub reorg_conf: u32,
}

/// Timeout budget for the swap.
///
/// The wallet enforces:
///   effective_gap >= claim_budget_seconds + reorg_margin_seconds + local_safety
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimeoutPolicy {
    /// External-chain lock duration in seconds.
    pub external_lock_seconds: u64,

    /// TSC-side lock duration in blocks.
    pub tsc_lock_blocks: u32,

    /// Budget (seconds) the claimer has for broadcasting + fee bumps.
    pub claim_budget_seconds: u64,

    /// Extra margin (seconds) to absorb reorg-related delays.
    pub reorg_margin_seconds: u64,

    /// Minimum gap (seconds) between external refund window and TSC refund.
    /// The wallet must reject swaps where the computed gap is smaller.
    pub min_timeout_gap_seconds: u64,
}

/// Fee-handling policy for claim and refund transactions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FeePolicy {
    /// Strategy for claiming.
    pub claim_strategy: FeeStrategy,

    /// Strategy for refunding.
    pub refund_strategy: FeeStrategy,

    /// How fee-bump funding is sourced.
    pub fee_funding_mode: FeeFundingMode,
}

/// Top-level cross-chain spot v1 contract payload.
///
/// This struct is serialized as JSON inside `Offer.contract_payload`
/// when the offer travels through the existing bulletin board.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CrossChainSpotV1Payload {
    /// Must be `"cross_chain_spot_v1"`.
    pub schema: String,

    /// Unique payload identifier.
    pub id: String,

    /// Maker or taker role of the poster.
    pub role: String,

    /// TSC-side leg.
    pub tsc_leg: TscLeg,

    /// External-chain leg.
    pub external_leg: ExternalLeg,

    /// Which side funds first.
    pub funding_order: FundingOrder,

    /// Confirmation thresholds.
    pub confirmation_policy: ConfirmationPolicy,

    /// Timeout budgets.
    pub timeout_policy: TimeoutPolicy,

    /// Fee-handling policy.
    pub fee_policy: FeePolicy,
}

impl CrossChainSpotV1Payload {
    /// Validate internal consistency of the payload.
    ///
    /// Returns `Ok(())` if the payload is well-formed, or an error string
    /// describing the first problem found.
    pub fn validate(&self) -> Result<(), String> {
        // Schema tag
        if self.schema != CROSS_CHAIN_SPOT_V1_SCHEMA {
            return Err(format!(
                "unexpected schema: expected '{}', got '{}'",
                CROSS_CHAIN_SPOT_V1_SCHEMA, self.schema
            ));
        }

        // ID must be non-empty
        if self.id.is_empty() {
            return Err("payload id must not be empty".to_string());
        }

        // Role
        if self.role != "maker" && self.role != "taker" {
            return Err(format!(
                "invalid role '{}': expected 'maker' or 'taker'",
                self.role
            ));
        }

        // Units must parse as positive decimal
        validate_units(&self.tsc_leg.units, "tsc_leg.units")?;
        validate_units(&self.external_leg.units, "external_leg.units")?;

        // Adapter must match chain
        match (&self.external_leg.chain, &self.external_leg.adapter) {
            (ExternalChain::Btc, AdapterKind::BtcScriptlessV1) => {}
            (ExternalChain::Ethereum, AdapterKind::EthHtlcV1) => {}
            (ExternalChain::Tron, AdapterKind::TronHtlcV1) => {}
            (chain, adapter) => {
                return Err(format!(
                    "adapter {:?} is not valid for chain {:?}",
                    adapter, chain
                ));
            }
        }

        // Timeout gap check
        let tp = &self.timeout_policy;
        let required_gap = tp.claim_budget_seconds + tp.reorg_margin_seconds;
        if tp.min_timeout_gap_seconds < required_gap {
            return Err(format!(
                "min_timeout_gap_seconds ({}) must be >= claim_budget_seconds ({}) + reorg_margin_seconds ({})",
                tp.min_timeout_gap_seconds, tp.claim_budget_seconds, tp.reorg_margin_seconds
            ));
        }

        // Confirmation thresholds must be non-zero (no zero-conf)
        let cp = &self.confirmation_policy;
        if cp.external_min_conf == 0 {
            return Err("external_min_conf must not be zero (no zero-conf)".to_string());
        }
        if cp.tsc_min_conf == 0 {
            return Err("tsc_min_conf must not be zero (no zero-conf)".to_string());
        }

        // External addresses must be structurally valid
        validate_external_address(
            &self.external_leg.chain,
            &self.external_leg.settlement_address,
        )
        .map_err(|e| format!("external_leg.settlement_address: {}", e))?;
        validate_external_address(&self.external_leg.chain, &self.external_leg.refund_address)
            .map_err(|e| format!("external_leg.refund_address: {}", e))?;

        // Fee funding mode must match adapter family
        match (
            &self.external_leg.adapter,
            &self.fee_policy.fee_funding_mode,
        ) {
            (AdapterKind::BtcScriptlessV1, FeeFundingMode::ReservedUtxo) => {}
            (AdapterKind::EthHtlcV1, FeeFundingMode::ReservedBalance) => {}
            (AdapterKind::TronHtlcV1, FeeFundingMode::ReservedBalance) => {}
            (adapter, mode) => {
                return Err(format!(
                    "fee_funding_mode {:?} is not expected for adapter {:?}",
                    mode, adapter
                ));
            }
        }

        Ok(())
    }
}

/// Validate that a units string is a positive decimal number.
///
/// Does NOT use f64 parsing — the entire point of decimal strings is to
/// avoid cross-language precision bugs. Positivity is checked by verifying
/// the string contains at least one non-zero digit and no negative sign.
fn validate_units(s: &str, field: &str) -> Result<(), String> {
    if s.is_empty() {
        return Err(format!("{} must not be empty", field));
    }
    // Must not start with minus
    if s.starts_with('-') {
        return Err(format!("{} '{}' must not be negative", field, s));
    }
    // Must be a valid non-negative decimal (integer or with decimal point)
    let valid = s.chars().all(|c| c.is_ascii_digit() || c == '.');
    if !valid || s.starts_with('.') || s.ends_with('.') || s.matches('.').count() > 1 {
        return Err(format!("{} '{}' is not a valid decimal string", field, s));
    }
    // Must contain at least one non-zero digit (reject "0", "0.0", "000", etc.)
    let has_nonzero_digit = s.chars().any(|c| c.is_ascii_digit() && c != '0');
    if !has_nonzero_digit {
        return Err(format!("{} must be positive", field));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Settlement profile (persisted in wallet)
// ---------------------------------------------------------------------------

/// External settlement profile stored in the wallet.
///
/// The user manages these profiles through the Qt settings UI.
/// Each profile represents one external chain endpoint the user
/// is willing to settle through.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SettlementProfile {
    /// Human-readable label (e.g. "My cold wallet").
    pub label: String,

    /// External chain this profile is for.
    pub chain: ExternalChain,

    /// Default settlement address on the external chain.
    pub address: String,

    /// Signing reference.
    /// - `"derived:auto"`: wallet derives keys from seed on a separate path
    /// - `"imported:<key-id>"`: externally imported signing material
    pub signer_ref: String,

    /// Preferred asset for this profile (e.g. `"BTC"`, `"ETH"`, `"USDT"`).
    pub preferred_asset: String,

    /// Fee speed preference.
    pub fee_speed: FeeSpeed,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_payload() -> CrossChainSpotV1Payload {
        CrossChainSpotV1Payload {
            schema: CROSS_CHAIN_SPOT_V1_SCHEMA.to_string(),
            id: "test-uuid-1234".to_string(),
            role: "maker".to_string(),
            tsc_leg: TscLeg {
                asset_id: "native".to_string(),
                units: "100000000".to_string(),
            },
            external_leg: ExternalLeg {
                chain: ExternalChain::Btc,
                asset: "BTC".to_string(),
                units: "100000000".to_string(),
                settlement_address: "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4".to_string(),
                refund_address: "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4".to_string(),
                adapter: AdapterKind::BtcScriptlessV1,
            },
            funding_order: FundingOrder::TscFirst,
            confirmation_policy: ConfirmationPolicy {
                external_min_conf: 6,
                tsc_min_conf: 1,
                reorg_conf: 6,
            },
            timeout_policy: TimeoutPolicy {
                external_lock_seconds: 86400,
                tsc_lock_blocks: 288,
                claim_budget_seconds: 21600,
                reorg_margin_seconds: 3600,
                min_timeout_gap_seconds: 25200,
            },
            fee_policy: FeePolicy {
                claim_strategy: FeeStrategy::RbfOrCpfp,
                refund_strategy: FeeStrategy::RbfOrCpfp,
                fee_funding_mode: FeeFundingMode::ReservedUtxo,
            },
        }
    }

    #[test]
    fn test_valid_payload() {
        let payload = sample_payload();
        assert!(payload.validate().is_ok());
    }

    #[test]
    fn test_roundtrip_json() {
        let payload = sample_payload();
        let json = serde_json::to_string(&payload).unwrap();
        let parsed: CrossChainSpotV1Payload = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.schema, CROSS_CHAIN_SPOT_V1_SCHEMA);
        assert_eq!(parsed.external_leg.chain, ExternalChain::Btc);
        assert_eq!(parsed.funding_order, FundingOrder::TscFirst);
    }

    #[test]
    fn test_wrong_schema() {
        let mut payload = sample_payload();
        payload.schema = "wrong".to_string();
        assert!(payload
            .validate()
            .unwrap_err()
            .contains("unexpected schema"));
    }

    #[test]
    fn test_zero_conf_rejected() {
        let mut payload = sample_payload();
        payload.confirmation_policy.external_min_conf = 0;
        assert!(payload.validate().unwrap_err().contains("zero-conf"));
    }

    #[test]
    fn test_timeout_gap_too_small() {
        let mut payload = sample_payload();
        payload.timeout_policy.min_timeout_gap_seconds = 100; // way too small
        assert!(payload
            .validate()
            .unwrap_err()
            .contains("min_timeout_gap_seconds"));
    }

    #[test]
    fn test_adapter_chain_mismatch() {
        let mut payload = sample_payload();
        payload.external_leg.adapter = AdapterKind::EthHtlcV1;
        // chain is still BTC
        assert!(payload
            .validate()
            .unwrap_err()
            .contains("not valid for chain"));
    }

    #[test]
    fn test_fee_mode_mismatch() {
        let mut payload = sample_payload();
        payload.fee_policy.fee_funding_mode = FeeFundingMode::ReservedBalance;
        // BTC adapter expects ReservedUtxo
        assert!(payload
            .validate()
            .unwrap_err()
            .contains("not expected for adapter"));
    }

    #[test]
    fn test_invalid_units() {
        let mut payload = sample_payload();
        payload.tsc_leg.units = "0".to_string();
        assert!(payload.validate().unwrap_err().contains("positive"));

        payload.tsc_leg.units = "0.0".to_string();
        assert!(payload.validate().unwrap_err().contains("positive"));

        payload.tsc_leg.units = "000".to_string();
        assert!(payload.validate().unwrap_err().contains("positive"));

        payload.tsc_leg.units = "-100".to_string();
        assert!(payload.validate().unwrap_err().contains("negative"));

        payload.tsc_leg.units = "".to_string();
        assert!(payload.validate().unwrap_err().contains("empty"));
    }

    #[test]
    fn test_large_precision_units() {
        // These values would lose precision as f64 — the old code would break
        let mut payload = sample_payload();
        payload.tsc_leg.units = "99999999999999999".to_string();
        assert!(payload.validate().is_ok());

        payload.tsc_leg.units = "0.000000000000000001".to_string();
        assert!(payload.validate().is_ok());
    }

    #[test]
    fn test_eth_payload() {
        let mut payload = sample_payload();
        payload.external_leg.chain = ExternalChain::Ethereum;
        payload.external_leg.asset = "USDT".to_string();
        payload.external_leg.adapter = AdapterKind::EthHtlcV1;
        // Use all-lowercase to avoid EIP-55 check dependency in this test
        payload.external_leg.settlement_address =
            "0xd8da6bf26964af9d7eed9e03e53415d37aa96045".to_string();
        payload.external_leg.refund_address =
            "0xd8da6bf26964af9d7eed9e03e53415d37aa96045".to_string();
        payload.funding_order = FundingOrder::ExternalFirst;
        payload.fee_policy.claim_strategy = FeeStrategy::GasEscalator;
        payload.fee_policy.refund_strategy = FeeStrategy::GasEscalator;
        payload.fee_policy.fee_funding_mode = FeeFundingMode::ReservedBalance;
        assert!(payload.validate().is_ok());
    }

    #[test]
    fn test_bad_settlement_address_rejected() {
        let mut payload = sample_payload();
        payload.external_leg.settlement_address = "not-a-real-address".to_string();
        let err = payload.validate().unwrap_err();
        assert!(err.contains("settlement_address"), "error was: {}", err);
    }

    #[test]
    fn test_bad_refund_address_rejected() {
        let mut payload = sample_payload();
        payload.external_leg.refund_address = "".to_string();
        let err = payload.validate().unwrap_err();
        assert!(err.contains("refund_address"), "error was: {}", err);
    }

    #[test]
    fn test_empty_settlement_address_rejected() {
        let mut payload = sample_payload();
        payload.external_leg.settlement_address = "".to_string();
        let err = payload.validate().unwrap_err();
        assert!(err.contains("settlement_address"), "error was: {}", err);
    }

    #[test]
    fn test_settlement_profile_roundtrip() {
        let profile = SettlementProfile {
            label: "My BTC wallet".to_string(),
            chain: ExternalChain::Btc,
            address: "bc1qtest".to_string(),
            signer_ref: "derived:auto".to_string(),
            preferred_asset: "BTC".to_string(),
            fee_speed: FeeSpeed::Normal,
        };
        let json = serde_json::to_string(&profile).unwrap();
        let parsed: SettlementProfile = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.chain, ExternalChain::Btc);
        assert_eq!(parsed.signer_ref, "derived:auto");
    }
}
