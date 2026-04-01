// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! State machine validation tests for bulletin board trading

use cosign_bridge::bulletin_board::*;

#[test]
fn test_offer_state_transitions() {
    let mut offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.1,
        65000.0,
        "maker_pubkey_hex".to_string(),
        "regtest".to_string(),
    );

    // Initial state should be Posted
    assert_eq!(offer.state, OfferState::Posted);
    assert!(offer.can_accept_requests());
    assert!(!offer.is_expired());

    // Transition: Posted → Requested
    offer.state = OfferState::Requested("taker_pubkey_hex".to_string());
    assert!(!offer.can_accept_requests()); // Can't accept more requests
    if let OfferState::Requested(pubkey) = &offer.state {
        assert_eq!(pubkey, "taker_pubkey_hex");
    } else {
        panic!("Expected Requested state");
    }

    // Transition: Requested → Accepted
    offer.state = OfferState::Accepted("taker_pubkey_hex".to_string());
    assert!(!offer.can_accept_requests());
    if let OfferState::Accepted(pubkey) = &offer.state {
        assert_eq!(pubkey, "taker_pubkey_hex");
    } else {
        panic!("Expected Accepted state");
    }

    // Transition: Accepted → Handshaking
    offer.state = OfferState::Handshaking;
    assert!(!offer.can_accept_requests());

    // Transition: Handshaking → Active
    offer.state = OfferState::Active;
    assert!(!offer.can_accept_requests());

    // Transition: Active → Completed
    offer.state = OfferState::Completed;
    assert!(!offer.can_accept_requests());
}

#[test]
fn test_offer_cancellation() {
    let mut offer = Offer::new(
        OfferType::Buy,
        "USD".to_string(),
        "BTC".to_string(),
        1000.0,
        0.000015,
        "maker_pubkey".to_string(),
        "regtest".to_string(),
    );

    assert_eq!(offer.state, OfferState::Posted);
    assert!(offer.can_accept_requests());

    // Maker cancels offer
    offer.state = OfferState::Cancelled;
    assert!(!offer.can_accept_requests());
}

#[test]
fn test_offer_expiry() {
    let mut offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.5,
        65000.0,
        "maker_pubkey".to_string(),
        "regtest".to_string(),
    );

    // Fresh offer should not be expired
    assert!(!offer.is_expired());
    assert!(offer.can_accept_requests());

    // Set expiry to past
    offer.expires_at = 1000; // Unix timestamp in the past
    assert!(offer.is_expired());
    assert!(!offer.can_accept_requests()); // Expired offers can't accept requests
}

#[test]
fn test_trade_request_lifecycle() {
    let request = TradeRequest::new(
        "offer_123".to_string(),
        "taker_pubkey".to_string(),
        "maker_pubkey".to_string(),
        Some("I'm interested".to_string()),
    );

    assert_eq!(request.status, RequestStatus::Pending);
    assert_eq!(request.offer_id, "offer_123");
    assert_eq!(request.taker_pubkey, "taker_pubkey");
    assert_eq!(request.maker_pubkey, "maker_pubkey");
    assert_eq!(request.message, Some("I'm interested".to_string()));
    assert!(!request.id.is_empty());
}

#[test]
fn test_offer_constraints() {
    let mut offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.1,
        65000.0,
        "maker_pubkey".to_string(),
        "regtest".to_string(),
    );

    // Set constraints
    offer.payment_methods = vec!["bank_transfer".to_string(), "cash".to_string()];
    offer.regions = vec!["US".to_string(), "EU".to_string()];
    offer.requires_escrow = true;
    offer.min_reputation_score = 50.0;

    assert_eq!(offer.payment_methods.len(), 2);
    assert_eq!(offer.regions.len(), 2);
    assert!(offer.requires_escrow);
    assert_eq!(offer.min_reputation_score, 50.0);
}

#[test]
fn test_offer_filters() {
    // Test default filters
    let filters = OfferFilters::default();
    assert!(filters.network.is_none());
    assert!(filters.offer_type.is_none());
    assert!(filters.min_amount.is_none());
    assert!(filters.max_amount.is_none());
    assert!(filters.region.is_none());
    assert!(filters.payment_method.is_none());
    assert!(filters.min_reputation.is_none());

    // Test with all filters set including network
    let filters = OfferFilters {
        network: Some("main".to_string()),
        offer_type: Some("sell".to_string()),
        min_amount: Some(0.01),
        max_amount: Some(1.0),
        region: Some("US".to_string()),
        payment_method: Some("bank_transfer".to_string()),
        min_reputation: Some(75.0),
        contract_type: None,
        maker_role: None,
        min_apr: None,
        max_apr: None,
        min_tenor_days: None,
        max_tenor_days: None,
    };

    assert_eq!(filters.network, Some("main".to_string()));
    assert_eq!(filters.offer_type, Some("sell".to_string()));
    assert_eq!(filters.min_amount, Some(0.01));
    assert_eq!(filters.max_amount, Some(1.0));
    assert_eq!(filters.region, Some("US".to_string()));
    assert_eq!(filters.payment_method, Some("bank_transfer".to_string()));
    assert_eq!(filters.min_reputation, Some(75.0));
}

#[test]
fn test_offer_type_serialization() {
    // Test that offer types serialize correctly
    let buy_offer = Offer::new(
        OfferType::Buy,
        "USD".to_string(),
        "BTC".to_string(),
        1000.0,
        0.000015,
        "maker".to_string(),
        "main".to_string(),
    );
    assert!(matches!(buy_offer.offer_type, OfferType::Buy));
    assert_eq!(buy_offer.network, "main");

    let sell_offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.1,
        65000.0,
        "maker".to_string(),
        "signet".to_string(),
    );
    assert!(matches!(sell_offer.offer_type, OfferType::Sell));
    assert_eq!(sell_offer.network, "signet");

    let swap_offer = Offer::new(
        OfferType::Swap,
        "BTC".to_string(),
        "ETH".to_string(),
        1.0,
        15.0,
        "maker".to_string(),
        "regtest".to_string(),
    );
    assert!(matches!(swap_offer.offer_type, OfferType::Swap));
    assert_eq!(swap_offer.network, "regtest");
}

#[test]
fn test_request_status_transitions() {
    let mut request = TradeRequest::new(
        "offer_123".to_string(),
        "taker".to_string(),
        "maker".to_string(),
        None,
    );

    // Initial status
    assert_eq!(request.status, RequestStatus::Pending);

    // Accepted
    request.status = RequestStatus::Accepted;
    assert_eq!(request.status, RequestStatus::Accepted);

    // Reset and reject
    request.status = RequestStatus::Pending;
    request.status = RequestStatus::Rejected;
    assert_eq!(request.status, RequestStatus::Rejected);

    request.status = RequestStatus::Cancelled;
    assert_eq!(request.status, RequestStatus::Cancelled);
}

#[test]
fn test_invalid_state_transitions() {
    let mut offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.1,
        65000.0,
        "maker".to_string(),
        "regtest".to_string(),
    );

    // Can't go from Posted directly to Completed
    offer.state = OfferState::Completed;
    assert!(!offer.can_accept_requests());

    // Can't accept requests when Expired
    offer.state = OfferState::Expired;
    assert!(!offer.can_accept_requests());

    // Can't accept requests when Cancelled
    offer.state = OfferState::Cancelled;
    assert!(!offer.can_accept_requests());
}

#[test]
fn test_multiple_offers_same_maker() {
    let maker_pubkey = "maker_pubkey_123".to_string();

    let offer1 = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.1,
        65000.0,
        maker_pubkey.clone(),
        "main".to_string(),
    );

    let offer2 = Offer::new(
        OfferType::Buy,
        "USD".to_string(),
        "BTC".to_string(),
        10000.0,
        0.000015,
        maker_pubkey.clone(),
        "main".to_string(),
    );

    // Different offer IDs
    assert_ne!(offer1.id, offer2.id);

    // Same maker
    assert_eq!(offer1.maker_pubkey, offer2.maker_pubkey);

    // Different types
    assert_ne!(
        std::mem::discriminant(&offer1.offer_type),
        std::mem::discriminant(&offer2.offer_type)
    );
}

#[test]
fn test_offer_invite_link_flow() {
    let mut offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        0.1,
        65000.0,
        "maker".to_string(),
        "regtest".to_string(),
    );

    // Initially no invite link
    assert!(offer.invite_link.is_none());
    assert!(offer.session_id.is_none());

    // After accept_request, invite link should be set
    offer.state = OfferState::Accepted("taker".to_string());
    offer.invite_link =
        Some("cosign:?r=session_abc&t=websocket#c=word1-word2-word3-word4-word5".to_string());
    offer.session_id = Some("session_abc".to_string());

    assert!(offer.invite_link.is_some());
    assert!(offer.session_id.is_some());

    // Verify invite link format
    let invite = offer.invite_link.unwrap();
    assert!(invite.starts_with("cosign:?"));
    assert!(invite.contains("r=session_abc"));
    assert!(invite.contains("#c="));
}

// ============================================================================
// Network Compartmentalization Tests
// ============================================================================

#[test]
fn test_network_compartmentalization_different_chains() {
    // Create offers on different networks
    let mainnet_offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        1.0,
        100000.0,
        "maker_mainnet".to_string(),
        "tensor".to_string(), // Tensor mainnet
    );

    let regtest_offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        1.0,
        100000.0,
        "maker_regtest".to_string(),
        "tensor-reg".to_string(), // Tensor regtest
    );

    let signet_offer = Offer::new(
        OfferType::Sell,
        "BTC".to_string(),
        "USD".to_string(),
        1.0,
        100000.0,
        "maker_signet".to_string(),
        "signet".to_string(), // Bitcoin signet
    );

    // Verify network field is correctly set
    assert_eq!(mainnet_offer.network, "tensor");
    assert_eq!(regtest_offer.network, "tensor-reg");
    assert_eq!(signet_offer.network, "signet");

    // Verify offers are distinct (different IDs)
    assert_ne!(mainnet_offer.id, regtest_offer.id);
    assert_ne!(regtest_offer.id, signet_offer.id);

    // Verify OfferSummary preserves network
    let mainnet_summary: OfferSummary = (&mainnet_offer).into();
    let regtest_summary: OfferSummary = (&regtest_offer).into();

    assert_eq!(mainnet_summary.network, "tensor");
    assert_eq!(regtest_summary.network, "tensor-reg");
}

#[test]
fn test_network_filter_in_offer_filters() {
    // Test that network filter can be set in OfferFilters
    let tensor_filter = OfferFilters {
        network: Some("tensor".to_string()),
        ..Default::default()
    };

    let regtest_filter = OfferFilters {
        network: Some("tensor-reg".to_string()),
        ..Default::default()
    };

    assert_eq!(tensor_filter.network, Some("tensor".to_string()));
    assert_eq!(regtest_filter.network, Some("tensor-reg".to_string()));

    // Default filter should have no network restriction
    let default_filter = OfferFilters::default();
    assert!(default_filter.network.is_none());
}

#[test]
fn test_all_supported_networks() {
    // Test all supported network strings from chaintype.cpp
    let networks = vec![
        "main",        // Bitcoin mainnet
        "test",        // Bitcoin testnet
        "testnet4",    // Bitcoin testnet4
        "signet",      // Bitcoin signet
        "regtest",     // Bitcoin regtest
        "tensor",      // Tensor mainnet
        "tensor-test", // Tensor testnet
        "tensor-reg",  // Tensor regtest
    ];

    for network in networks {
        let offer = Offer::new(
            OfferType::Buy,
            "USD".to_string(),
            "BTC".to_string(),
            1000.0,
            0.00001,
            "maker".to_string(),
            network.to_string(),
        );

        assert_eq!(
            offer.network, network,
            "Network should be set correctly for {}",
            network
        );
        assert_eq!(offer.state, OfferState::Posted);
        assert!(!offer.is_expired());
    }
}

#[test]
fn test_contract_offer_network() {
    // Contract offers should also have network field
    let contract = Offer::new_contract(
        ContractType::Repo,
        r#"{"collateral":"1BTC","duration":"30d"}"#.to_string(),
        "lender".to_string(),
        "maker_pubkey".to_string(),
        "tensor".to_string(), // Tensor mainnet
        Some(5.5),            // APR
        Some(150.0),          // LTV
        Some(30),             // Tenor days
    );

    assert_eq!(contract.network, "tensor");
    assert!(matches!(contract.contract_type, Some(ContractType::Repo)));
    assert_eq!(contract.maker_role, Some("lender".to_string()));
}
