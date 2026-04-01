// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Adapter-neutral cross-chain execution state machine.
//!
//! The state machine is chain-agnostic at the top level.
//! Adapter-specific sub-state hangs beneath these states but
//! Qt renders a chain-agnostic lifecycle.

use serde::{Deserialize, Serialize};

/// Top-level cross-chain swap execution state.
///
/// Secret revelation is a distinct gated transition, never an
/// incidental side effect of "continue".
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CrossChainState {
    /// Offer drafted but not yet posted.
    Draft,

    /// Offer posted to bulletin board.
    Posted,

    /// Taker matched with maker.
    Matched,

    /// Cosign session established.
    SessionEstablished,

    /// Both sides agreed on final swap terms (payload, addresses, policies).
    TermsFinalized,

    /// Pre-signatures exchanged, funding transactions prepared.
    FundingPrepared,

    /// Counterparty lock transaction seen in mempool (not yet confirmed).
    CounterpartyLockSeen,

    /// Counterparty lock confirmed to policy threshold.
    CounterpartyLockConfirmed,

    /// Our own lock confirmed to policy threshold.
    LocalLockConfirmed,

    /// All preconditions met for claim; secret not yet revealed.
    ClaimReady,

    /// Claim transaction broadcast (secret now revealed on-chain).
    ClaimBroadcast,

    /// Emergency: secret was revealed but counterparty leg deteriorated.
    /// Wallet must prioritize aggressive claim recovery with fee bumps.
    EmergencyClaim,

    /// Claim transaction confirmed.
    ClaimConfirmed,

    /// Timeout reached; refund is available.
    RefundReady,

    /// Refund transaction broadcast.
    RefundBroadcast,

    /// Refund confirmed; funds recovered.
    Refunded,

    /// Both legs settled; swap complete.
    Completed,

    /// Swap aborted before secret revelation (mutual or unilateral).
    Aborted,
}

impl CrossChainState {
    /// Whether this state is terminal (no further transitions expected).
    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Completed | Self::Refunded | Self::Aborted)
    }

    /// Whether the adaptor secret has been revealed in this state.
    ///
    /// Once the secret is out, the swap cannot safely abort — it must
    /// either complete or enter emergency claim.
    pub fn secret_revealed(&self) -> bool {
        matches!(
            self,
            Self::ClaimBroadcast | Self::EmergencyClaim | Self::ClaimConfirmed | Self::Completed
        )
    }

    /// Whether this is a funded state (at least one leg has been funded).
    pub fn is_funded(&self) -> bool {
        !matches!(
            self,
            Self::Draft
                | Self::Posted
                | Self::Matched
                | Self::SessionEstablished
                | Self::TermsFinalized
                | Self::FundingPrepared
        )
    }

    /// Returns the set of valid successor states from this state.
    ///
    /// Key invariants:
    /// - **Pre-funding** states may reach `Aborted` directly (nothing to recover).
    /// - **Counterparty-only-funded** states (`CounterpartyLockSeen`,
    ///   `CounterpartyLockConfirmed`) may reach `Aborted` — we haven't funded,
    ///   the counterparty handles their own refund independently.
    /// - **We-funded** states (`LocalLockConfirmed`, `ClaimReady`) must route
    ///   through `RefundReady -> RefundBroadcast -> Refunded` to terminate
    ///   without completing. Direct `Aborted` is forbidden because our
    ///   funding output must be recovered first.
    /// - **Secret-revealed** states cannot abort or refund — only forward
    ///   to `ClaimConfirmed` / `Completed` or `EmergencyClaim`.
    fn valid_successors(&self) -> &'static [CrossChainState] {
        match self {
            // Pre-funding: safe to abort outright
            Self::Draft => &[Self::Posted, Self::Aborted],
            Self::Posted => &[Self::Matched, Self::Aborted],
            Self::Matched => &[Self::SessionEstablished, Self::Aborted],
            Self::SessionEstablished => &[Self::TermsFinalized, Self::Aborted],
            Self::TermsFinalized => &[Self::FundingPrepared, Self::Aborted],
            Self::FundingPrepared => &[
                Self::CounterpartyLockSeen,
                Self::LocalLockConfirmed,
                Self::Aborted,
            ],

            // Counterparty-only funded: we haven't funded yet, safe to abort
            Self::CounterpartyLockSeen => &[Self::CounterpartyLockConfirmed, Self::Aborted],
            Self::CounterpartyLockConfirmed => &[
                Self::LocalLockConfirmed,
                Self::ClaimReady,
                Self::RefundReady,
                Self::Aborted,
            ],

            // We-funded: must go through refund path to terminate, no direct Aborted
            Self::LocalLockConfirmed => &[
                Self::CounterpartyLockSeen,
                Self::ClaimReady,
                Self::RefundReady,
            ],
            Self::ClaimReady => &[Self::ClaimBroadcast, Self::RefundReady],

            // Secret revealed: no abort, no refund — only forward or emergency
            Self::ClaimBroadcast => &[Self::ClaimConfirmed, Self::EmergencyClaim, Self::Completed],
            Self::EmergencyClaim => &[Self::ClaimConfirmed, Self::Completed],
            Self::ClaimConfirmed => &[Self::Completed],

            // Refund path: must complete through Refunded
            Self::RefundReady => &[Self::RefundBroadcast],
            Self::RefundBroadcast => &[Self::Refunded],

            // Terminal states — no successors
            Self::Refunded => &[],
            Self::Completed => &[],
            Self::Aborted => &[],
        }
    }
}

impl std::fmt::Display for CrossChainState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let label = match self {
            Self::Draft => "Draft",
            Self::Posted => "Posted",
            Self::Matched => "Matched",
            Self::SessionEstablished => "Session Established",
            Self::TermsFinalized => "Terms Finalized",
            Self::FundingPrepared => "Funding Prepared",
            Self::CounterpartyLockSeen => "Counterparty Lock Seen",
            Self::CounterpartyLockConfirmed => "Counterparty Lock Confirmed",
            Self::LocalLockConfirmed => "Local Lock Confirmed",
            Self::ClaimReady => "Claim Ready",
            Self::ClaimBroadcast => "Claim Broadcast",
            Self::EmergencyClaim => "Emergency Claim",
            Self::ClaimConfirmed => "Claim Confirmed",
            Self::RefundReady => "Refund Ready",
            Self::RefundBroadcast => "Refund Broadcast",
            Self::Refunded => "Refunded",
            Self::Completed => "Completed",
            Self::Aborted => "Aborted",
        };
        write!(f, "{}", label)
    }
}

/// Persisted cross-chain execution record.
///
/// Contains everything the wallet needs to resume a swap after crash
/// without requiring a live cosign session.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CrossChainExecutionRecord {
    /// Swap identifier (matches `CrossChainSpotV1Payload.id`).
    pub swap_id: String,

    /// Bulletin board offer id.
    pub offer_id: String,

    /// Current execution state.
    pub state: CrossChainState,

    /// Serialized payload snapshot (the agreed terms).
    pub payload_json: String,

    /// Our role: `"maker"` or `"taker"`.
    pub local_role: String,

    /// Counterparty's Nostr public key.
    pub counterparty_pubkey: String,

    // -- Funding artifacts --
    /// TSC funding transaction id (if broadcast).
    pub tsc_funding_txid: Option<String>,

    /// External funding transaction id (if broadcast).
    pub external_funding_txid: Option<String>,

    // -- Secret binding --
    /// Adaptor secret binding reference (encrypted in wallet storage).
    /// This is a key-id, not the raw secret.
    pub adaptor_secret_ref: Option<String>,

    // -- Refund artifacts --
    /// Serialized refund transaction or package (for crash recovery).
    pub refund_artifact: Option<String>,

    // -- Confirmation tracking --
    /// Last observed confirmation depth on external chain.
    pub external_conf_depth: u32,

    /// Last observed confirmation depth on TSC.
    pub tsc_conf_depth: u32,

    // -- Fee tracking --
    /// Current fee level index (0 = normal, 1 = bumped, 2 = aggressive).
    pub fee_escalation_level: u32,

    // -- Oracle (ETH/TRON only) --
    /// Serialized oracle attestation snapshot.
    pub oracle_attestation: Option<String>,

    // -- Timestamps --
    /// Unix timestamp when execution record was created.
    pub created_at: u64,

    /// Unix timestamp of last state transition.
    pub updated_at: u64,
}

impl CrossChainExecutionRecord {
    /// Create a new execution record at the `Draft` state.
    pub fn new(
        swap_id: String,
        offer_id: String,
        payload_json: String,
        local_role: String,
        counterparty_pubkey: String,
    ) -> Self {
        let now = chrono::Utc::now().timestamp() as u64;
        Self {
            swap_id,
            offer_id,
            state: CrossChainState::Draft,
            payload_json,
            local_role,
            counterparty_pubkey,
            tsc_funding_txid: None,
            external_funding_txid: None,
            adaptor_secret_ref: None,
            refund_artifact: None,
            external_conf_depth: 0,
            tsc_conf_depth: 0,
            fee_escalation_level: 0,
            oracle_attestation: None,
            created_at: now,
            updated_at: now,
        }
    }

    /// Transition to a new state, updating the timestamp.
    ///
    /// Enforces the adjacency graph defined by `CrossChainState::valid_successors()`.
    /// Only transitions listed in that graph are allowed. This prevents impossible
    /// jumps like `Draft -> ClaimBroadcast`.
    ///
    /// Returns `Err` if the transition is not allowed.
    pub fn transition_to(&mut self, new_state: CrossChainState) -> Result<(), String> {
        // Cannot leave a terminal state
        if self.state.is_terminal() {
            return Err(format!(
                "cannot transition from terminal state '{}'",
                self.state
            ));
        }

        // Enforce adjacency: new_state must be in valid_successors
        if !self.state.valid_successors().contains(&new_state) {
            return Err(format!(
                "invalid transition: {} -> {} (valid successors: {:?})",
                self.state,
                new_state,
                self.state
                    .valid_successors()
                    .iter()
                    .map(|s| s.to_string())
                    .collect::<Vec<_>>()
            ));
        }

        self.state = new_state;
        self.updated_at = chrono::Utc::now().timestamp() as u64;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn new_record(id: &str) -> CrossChainExecutionRecord {
        CrossChainExecutionRecord::new(
            id.to_string(),
            format!("offer-{}", id),
            "{}".to_string(),
            "maker".to_string(),
            "npub1counter".to_string(),
        )
    }

    #[test]
    fn test_state_properties() {
        assert!(!CrossChainState::Draft.is_terminal());
        assert!(!CrossChainState::Draft.secret_revealed());
        assert!(!CrossChainState::Draft.is_funded());

        assert!(CrossChainState::Completed.is_terminal());
        assert!(CrossChainState::Completed.secret_revealed());

        assert!(CrossChainState::ClaimBroadcast.secret_revealed());
        assert!(CrossChainState::EmergencyClaim.secret_revealed());
        assert!(!CrossChainState::ClaimReady.secret_revealed());

        assert!(CrossChainState::CounterpartyLockSeen.is_funded());
        assert!(!CrossChainState::FundingPrepared.is_funded());
    }

    #[test]
    fn test_terminal_no_transition() {
        let mut record = new_record("1");
        record.state = CrossChainState::Completed;
        assert!(record.transition_to(CrossChainState::Draft).is_err());

        record.state = CrossChainState::Refunded;
        assert!(record.transition_to(CrossChainState::Draft).is_err());

        record.state = CrossChainState::Aborted;
        assert!(record.transition_to(CrossChainState::Draft).is_err());
    }

    #[test]
    fn test_no_secret_unreveal() {
        let mut record = new_record("2");
        record.state = CrossChainState::ClaimBroadcast;
        // Can go to EmergencyClaim (valid successor, still secret-revealed)
        assert!(record
            .transition_to(CrossChainState::EmergencyClaim)
            .is_ok());

        // Reset — ClaimBroadcast cannot go back to ClaimReady
        record.state = CrossChainState::ClaimBroadcast;
        assert!(record.transition_to(CrossChainState::ClaimReady).is_err());

        // Can go to terminal (Completed is a valid successor)
        record.state = CrossChainState::ClaimBroadcast;
        assert!(record.transition_to(CrossChainState::Completed).is_ok());
    }

    #[test]
    fn test_impossible_jumps_rejected() {
        let mut record = new_record("3");
        // Draft -> ClaimBroadcast: must be rejected
        assert!(record
            .transition_to(CrossChainState::ClaimBroadcast)
            .is_err());

        // Draft -> Completed: must be rejected
        assert!(record.transition_to(CrossChainState::Completed).is_err());

        // Draft -> CounterpartyLockConfirmed: must be rejected
        assert!(record
            .transition_to(CrossChainState::CounterpartyLockConfirmed)
            .is_err());

        // FundingPrepared -> ClaimReady: must be rejected (skips lock phases)
        record.state = CrossChainState::FundingPrepared;
        assert!(record.transition_to(CrossChainState::ClaimReady).is_err());

        // Posted -> TermsFinalized: must be rejected (skips Matched + SessionEstablished)
        record.state = CrossChainState::Posted;
        assert!(record
            .transition_to(CrossChainState::TermsFinalized)
            .is_err());
    }

    #[test]
    fn test_normal_lifecycle() {
        let mut record = new_record("4");

        let steps = vec![
            CrossChainState::Posted,
            CrossChainState::Matched,
            CrossChainState::SessionEstablished,
            CrossChainState::TermsFinalized,
            CrossChainState::FundingPrepared,
            CrossChainState::CounterpartyLockSeen,
            CrossChainState::CounterpartyLockConfirmed,
            CrossChainState::LocalLockConfirmed,
            CrossChainState::ClaimReady,
            CrossChainState::ClaimBroadcast,
            CrossChainState::ClaimConfirmed,
            CrossChainState::Completed,
        ];

        for state in steps {
            assert!(
                record.transition_to(state.clone()).is_ok(),
                "transition to {} should succeed",
                state
            );
        }
    }

    #[test]
    fn test_refund_lifecycle() {
        let mut record = new_record("5");
        // Walk to CounterpartyLockConfirmed via valid path
        record.state = CrossChainState::CounterpartyLockConfirmed;
        assert!(record.transition_to(CrossChainState::RefundReady).is_ok());
        assert!(record
            .transition_to(CrossChainState::RefundBroadcast)
            .is_ok());
        assert!(record.transition_to(CrossChainState::Refunded).is_ok());
        assert!(record.state.is_terminal());
    }

    #[test]
    fn test_emergency_claim_lifecycle() {
        let mut record = new_record("6");
        record.state = CrossChainState::ClaimBroadcast;
        // ClaimBroadcast -> EmergencyClaim -> ClaimConfirmed -> Completed
        assert!(record
            .transition_to(CrossChainState::EmergencyClaim)
            .is_ok());
        assert!(record
            .transition_to(CrossChainState::ClaimConfirmed)
            .is_ok());
        assert!(record.transition_to(CrossChainState::Completed).is_ok());
    }

    #[test]
    fn test_abort_from_pre_funding() {
        // Can abort from any pre-funding state
        for start in &[
            CrossChainState::Draft,
            CrossChainState::Posted,
            CrossChainState::Matched,
            CrossChainState::SessionEstablished,
            CrossChainState::TermsFinalized,
            CrossChainState::FundingPrepared,
        ] {
            let mut record = new_record("7");
            record.state = start.clone();
            assert!(
                record.transition_to(CrossChainState::Aborted).is_ok(),
                "should be able to abort from {}",
                start
            );
        }
    }

    #[test]
    fn test_abort_from_counterparty_only_funded() {
        // Can abort when only counterparty has funded (we haven't)
        for start in &[
            CrossChainState::CounterpartyLockSeen,
            CrossChainState::CounterpartyLockConfirmed,
        ] {
            let mut record = new_record("8a");
            record.state = start.clone();
            assert!(
                record.transition_to(CrossChainState::Aborted).is_ok(),
                "should be able to abort from {} (counterparty-only funded)",
                start
            );
        }
    }

    #[test]
    fn test_cannot_abort_from_we_funded() {
        // Once we have funded, must go through refund path — no direct Aborted
        for start in &[
            CrossChainState::LocalLockConfirmed,
            CrossChainState::ClaimReady,
        ] {
            let mut record = new_record("8b");
            record.state = start.clone();
            assert!(
                record.transition_to(CrossChainState::Aborted).is_err(),
                "should NOT be able to abort from {} (we funded, must refund)",
                start
            );
        }
    }

    #[test]
    fn test_we_funded_must_refund() {
        // LocalLockConfirmed -> RefundReady -> RefundBroadcast -> Refunded
        let mut record = new_record("8c");
        record.state = CrossChainState::LocalLockConfirmed;
        assert!(record.transition_to(CrossChainState::RefundReady).is_ok());
        assert!(record
            .transition_to(CrossChainState::RefundBroadcast)
            .is_ok());
        assert!(record.transition_to(CrossChainState::Refunded).is_ok());
        assert!(record.state.is_terminal());
    }

    #[test]
    fn test_refund_ready_cannot_abort() {
        // RefundReady can only go to RefundBroadcast, not Aborted
        let mut record = new_record("8d");
        record.state = CrossChainState::RefundReady;
        assert!(record.transition_to(CrossChainState::Aborted).is_err());
        assert!(record
            .transition_to(CrossChainState::RefundBroadcast)
            .is_ok());
    }

    #[test]
    fn test_cannot_abort_after_secret_reveal() {
        let mut record = new_record("9");
        record.state = CrossChainState::ClaimBroadcast;
        assert!(record.transition_to(CrossChainState::Aborted).is_err());

        record.state = CrossChainState::EmergencyClaim;
        assert!(record.transition_to(CrossChainState::Aborted).is_err());
    }

    #[test]
    fn test_local_lock_first_path() {
        // FundingPrepared -> LocalLockConfirmed (we fund first)
        // -> CounterpartyLockSeen (they fund) is not directly reachable
        // -> ClaimReady is valid from LocalLockConfirmed
        let mut record = new_record("10");
        record.state = CrossChainState::FundingPrepared;
        assert!(record
            .transition_to(CrossChainState::LocalLockConfirmed)
            .is_ok());
        // From LocalLockConfirmed, can see counterparty lock
        assert!(record
            .transition_to(CrossChainState::CounterpartyLockSeen)
            .is_ok());
    }
}
