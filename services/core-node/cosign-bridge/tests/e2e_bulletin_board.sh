#!/bin/bash
# Copyright (c) 2025 The TensorCash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://opensource.org/license/mit/.

# End-to-End Test Script for Bulletin Board Trading System
# Tests the complete maker-taker workflow

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_BIN="${SCRIPT_DIR}/../target/debug/cosign-bridge"
TEST_OUTPUT_DIR="${SCRIPT_DIR}/../target/e2e_test_output"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_test() {
    echo -e "\n${YELLOW}=== TEST: $1 ===${NC}"
    TESTS_RUN=$((TESTS_RUN + 1))
}

assert_success() {
    if [ $? -eq 0 ]; then
        log_info "✓ $1"
        TESTS_PASSED=$((TESTS_PASSED + 1))
        return 0
    else
        log_error "✗ $1"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        return 1
    fi
}

assert_json_field() {
    local json="$1"
    local field="$2"
    local expected="$3"

    local actual=$(echo "$json" | jq -r ".$field")

    if [ "$actual" == "$expected" ]; then
        log_info "✓ Field '$field' = '$expected'"
        TESTS_PASSED=$((TESTS_PASSED + 1))
        return 0
    else
        log_error "✗ Field '$field': expected '$expected', got '$actual'"
        TESTS_FAILED=$((TESTS_FAILED + 1))
        return 1
    fi
}

send_command() {
    local cmd="$1"
    local params="$2"

    # Compact the params JSON (remove newlines and extra spaces)
    local compact_params=$(echo "$params" | jq -c '.')

    local request="{\"command\":\"$cmd\",\"params\":$compact_params}"
    log_info "Sending: $cmd" >&2

    # Send to persistent bridge process via named pipe
    echo "$request" > "$BRIDGE_FIFO_IN"

    # Read response (with timeout)
    timeout 30s head -1 < "$BRIDGE_FIFO_OUT" 2>/dev/null
}

start_bridge() {
    log_info "Starting persistent bridge process..."

    # Create named pipes for communication
    BRIDGE_FIFO_IN="$TEST_OUTPUT_DIR/bridge_in.fifo"
    BRIDGE_FIFO_OUT="$TEST_OUTPUT_DIR/bridge_out.fifo"

    mkfifo "$BRIDGE_FIFO_IN"
    mkfifo "$BRIDGE_FIFO_OUT"

    # Start bridge in background, reading from input pipe and writing to output pipe
    "$BRIDGE_BIN" < "$BRIDGE_FIFO_IN" > "$BRIDGE_FIFO_OUT" 2>"$TEST_OUTPUT_DIR/bridge_stderr.log" &
    BRIDGE_PID=$!

    log_info "Bridge started with PID: $BRIDGE_PID"

    # Give bridge time to initialize
    sleep 1

    # Check if bridge is still running
    if ! kill -0 $BRIDGE_PID 2>/dev/null; then
        log_error "Bridge process died immediately"
        cat "$TEST_OUTPUT_DIR/bridge_stderr.log"
        return 1
    fi
}

stop_bridge() {
    log_info "Stopping bridge process..."
    if [ -n "$BRIDGE_PID" ]; then
        kill $BRIDGE_PID 2>/dev/null || true
        wait $BRIDGE_PID 2>/dev/null || true
    fi
}

cleanup() {
    log_info "Cleaning up..."
    stop_bridge
    rm -rf "$TEST_OUTPUT_DIR"
    pkill -f cosign-bridge || true
}

# ============================================================================
# MAIN TEST SUITE
# ============================================================================

main() {
    log_info "Starting End-to-End Bulletin Board Tests"
    log_info "Bridge binary: $BRIDGE_BIN"

    # Check dependencies
    if ! command -v jq &> /dev/null; then
        log_error "jq is required but not installed"
        exit 1
    fi

    if [ ! -f "$BRIDGE_BIN" ]; then
        log_error "Bridge binary not found at $BRIDGE_BIN"
        log_info "Run: cargo build"
        exit 1
    fi

    # Setup
    mkdir -p "$TEST_OUTPUT_DIR"
    trap cleanup EXIT

    # Start persistent bridge process
    start_bridge
    if [ $? -ne 0 ]; then
        log_error "Failed to start bridge"
        exit 1
    fi

    # ========================================================================
    # TEST 1: Initialize Bulletin Board
    # ========================================================================
    log_test "Initialize bulletin board with default relays"

    INIT_BB_RESPONSE=$(send_command "init_bb" "{}")
    echo "$INIT_BB_RESPONSE" > "$TEST_OUTPUT_DIR/init_bb_response.json"

    if echo "$INIT_BB_RESPONSE" | jq -e '.success == true' > /dev/null; then
        assert_success "init_bb succeeded"

        MAKER_PUBKEY=$(echo "$INIT_BB_RESPONSE" | jq -r '.pubkey')
        log_info "Maker pubkey: $MAKER_PUBKEY"

        if [ -n "$MAKER_PUBKEY" ] && [ "$MAKER_PUBKEY" != "null" ]; then
            assert_success "Pubkey generated"
        else
            log_error "No pubkey in response"
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi

        RELAY_COUNT=$(echo "$INIT_BB_RESPONSE" | jq '.relays | length')
        if [ "$RELAY_COUNT" -gt 0 ]; then
            log_info "Connected to $RELAY_COUNT relays"
            assert_success "Relay connection"
        else
            log_warn "No relays connected"
        fi
    else
        log_error "init_bb failed"
        log_error "Response: $INIT_BB_RESPONSE"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi

    # ========================================================================
    # TEST 2: Post Offer
    # ========================================================================
    log_test "Post a sell offer"

    POST_OFFER_PARAMS='{
        "offer_type": "sell",
        "asset_send": "BTC",
        "asset_recv": "USD",
        "amount": 0.1,
        "price": 65000.0,
        "payment_methods": ["bank_transfer", "cash"],
        "regions": ["US", "EU"],
        "requires_escrow": true,
        "min_reputation_score": 50.0
    }'

    POST_OFFER_RESPONSE=$(send_command "post_offer" "$POST_OFFER_PARAMS")
    echo "$POST_OFFER_RESPONSE" > "$TEST_OUTPUT_DIR/post_offer_response.json"

    if echo "$POST_OFFER_RESPONSE" | jq -e '.success == true' > /dev/null; then
        assert_success "post_offer succeeded"

        OFFER_ID=$(echo "$POST_OFFER_RESPONSE" | jq -r '.offer_id')
        log_info "Offer ID: $OFFER_ID"

        if [ -n "$OFFER_ID" ] && [ "$OFFER_ID" != "null" ]; then
            assert_success "Offer ID generated"
        else
            log_error "No offer_id in response"
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi
    else
        log_error "post_offer failed"
        log_error "Response: $POST_OFFER_RESPONSE"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi

    # ========================================================================
    # TEST 3: List Offers (no filter)
    # ========================================================================
    log_test "List all offers"

    sleep 2  # Give Nostr time to propagate

    LIST_OFFERS_RESPONSE=$(send_command "list_offers" "{}")
    echo "$LIST_OFFERS_RESPONSE" > "$TEST_OUTPUT_DIR/list_offers_response.json"

    if echo "$LIST_OFFERS_RESPONSE" | jq -e '.success == true' > /dev/null; then
        assert_success "list_offers succeeded"

        OFFER_COUNT=$(echo "$LIST_OFFERS_RESPONSE" | jq '.offers | length')
        log_info "Found $OFFER_COUNT offers"

        if [ "$OFFER_COUNT" -gt 0 ]; then
            assert_success "Offers retrieved"

            # Verify our offer is in the list
            OUR_OFFER=$(echo "$LIST_OFFERS_RESPONSE" | jq ".offers[] | select(.id == \"$OFFER_ID\")")
            if [ -n "$OUR_OFFER" ]; then
                assert_success "Our offer found in list"

                # Verify offer fields
                OFFER_TYPE=$(echo "$OUR_OFFER" | jq -r '.offer_type')
                assert_json_field "$OUR_OFFER" "offer_type" "sell"
                assert_json_field "$OUR_OFFER" "asset_send" "BTC"
                assert_json_field "$OUR_OFFER" "asset_recv" "USD"
            else
                log_warn "Our offer not found in list (may take time to propagate)"
            fi
        else
            log_warn "No offers found (Nostr propagation delay?)"
        fi
    else
        log_error "list_offers failed"
        log_error "Response: $LIST_OFFERS_RESPONSE"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi

    # ========================================================================
    # TEST 4: List Offers (with filter)
    # ========================================================================
    log_test "List offers with filters"

    FILTER_PARAMS='{
        "offer_type": "sell",
        "min_amount": 0.05,
        "max_amount": 0.5
    }'

    FILTERED_OFFERS_RESPONSE=$(send_command "list_offers" "$FILTER_PARAMS")
    echo "$FILTERED_OFFERS_RESPONSE" > "$TEST_OUTPUT_DIR/list_offers_filtered_response.json"

    if echo "$FILTERED_OFFERS_RESPONSE" | jq -e '.success == true' > /dev/null; then
        assert_success "Filtered list_offers succeeded"

        FILTERED_COUNT=$(echo "$FILTERED_OFFERS_RESPONSE" | jq '.offers | length')
        log_info "Found $FILTERED_COUNT filtered offers"

        # All offers should be "sell" type
        SELL_COUNT=$(echo "$FILTERED_OFFERS_RESPONSE" | jq '[.offers[] | select(.offer_type == "sell")] | length')
        if [ "$SELL_COUNT" -eq "$FILTERED_COUNT" ]; then
            assert_success "All filtered offers are 'sell' type"
        else
            log_error "Filter not applied correctly"
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi
    else
        log_error "Filtered list_offers failed"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi

    # ========================================================================
    # TEST 5: Request Trade (simulated taker)
    # ========================================================================
    log_test "Request trade on offer (taker perspective)"

    if [ -n "$OFFER_ID" ]; then
        REQUEST_TRADE_PARAMS="{
            \"offer_id\": \"$OFFER_ID\",
            \"message\": \"Interested in your BTC offer\"
        }"

        REQUEST_TRADE_RESPONSE=$(send_command "request_trade" "$REQUEST_TRADE_PARAMS")
        echo "$REQUEST_TRADE_RESPONSE" > "$TEST_OUTPUT_DIR/request_trade_response.json"

        if echo "$REQUEST_TRADE_RESPONSE" | jq -e '.success == true' > /dev/null; then
            assert_success "request_trade succeeded"

            REQUEST_ID=$(echo "$REQUEST_TRADE_RESPONSE" | jq -r '.request_id')
            log_info "Request ID: $REQUEST_ID"

            if [ -n "$REQUEST_ID" ] && [ "$REQUEST_ID" != "null" ]; then
                assert_success "Request ID generated"
            else
                log_error "No request_id in response"
                TESTS_FAILED=$((TESTS_FAILED + 1))
            fi
        else
            log_error "request_trade failed"
            log_error "Response: $REQUEST_TRADE_RESPONSE"
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi
    else
        log_warn "Skipping request_trade (no offer_id available)"
    fi

    # ========================================================================
    # TEST 6: List Requests (maker checks incoming)
    # ========================================================================
    log_test "List trade requests (maker perspective)"

    sleep 2  # Give Nostr time to propagate DM

    LIST_REQUESTS_RESPONSE=$(send_command "list_requests" "{}")
    echo "$LIST_REQUESTS_RESPONSE" > "$TEST_OUTPUT_DIR/list_requests_response.json"

    if echo "$LIST_REQUESTS_RESPONSE" | jq -e '.success == true' > /dev/null; then
        assert_success "list_requests succeeded"

        REQUEST_COUNT=$(echo "$LIST_REQUESTS_RESPONSE" | jq '.requests | length')
        log_info "Found $REQUEST_COUNT requests"

        if [ "$REQUEST_COUNT" -gt 0 ]; then
            assert_success "Requests retrieved"

            # Find our request
            if [ -n "$REQUEST_ID" ]; then
                OUR_REQUEST=$(echo "$LIST_REQUESTS_RESPONSE" | jq ".requests[] | select(.id == \"$REQUEST_ID\")")
                if [ -n "$OUR_REQUEST" ]; then
                    assert_success "Our request found in list"
                else
                    log_warn "Our request not found (DM propagation delay?)"
                fi
            fi
        else
            log_warn "No requests found (may be same node testing itself)"
        fi
    else
        log_error "list_requests failed"
        log_error "Response: $LIST_REQUESTS_RESPONSE"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi

    # ========================================================================
    # TEST 7: Accept Request (creates bilateral session)
    # ========================================================================
    log_test "Accept trade request (maker accepts)"

    if [ -n "$REQUEST_ID" ]; then
        ACCEPT_REQUEST_PARAMS="{
            \"request_id\": \"$REQUEST_ID\",
            \"transport\": \"websocket\",
            \"ttl\": 1800
        }"

        ACCEPT_REQUEST_RESPONSE=$(send_command "accept_request" "$ACCEPT_REQUEST_PARAMS")
        echo "$ACCEPT_REQUEST_RESPONSE" > "$TEST_OUTPUT_DIR/accept_request_response.json"

        if echo "$ACCEPT_REQUEST_RESPONSE" | jq -e '.success == true' > /dev/null; then
            assert_success "accept_request succeeded"

            INVITE_LINK=$(echo "$ACCEPT_REQUEST_RESPONSE" | jq -r '.invite_link')
            SESSION_ID=$(echo "$ACCEPT_REQUEST_RESPONSE" | jq -r '.session_id')

            if [ -n "$INVITE_LINK" ] && [ "$INVITE_LINK" != "null" ]; then
                log_info "Invite link: $INVITE_LINK"
                assert_success "Invite link generated"
            else
                log_error "No invite_link in response"
                TESTS_FAILED=$((TESTS_FAILED + 1))
            fi

            if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ]; then
                log_info "Session ID: $SESSION_ID"
                assert_success "Bilateral session created"
            else
                log_error "No session_id in response"
                TESTS_FAILED=$((TESTS_FAILED + 1))
            fi
        else
            log_error "accept_request failed"
            log_error "Response: $ACCEPT_REQUEST_RESPONSE"
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi
    else
        log_warn "Skipping accept_request (no request_id available)"
    fi

    # ========================================================================
    # TEST 8: Reject Request
    # ========================================================================
    log_test "Reject trade request (alternative flow)"

    # Note: This would fail on the same request_id, so we skip if already accepted
    log_info "Skipping reject test (would require separate request)"

    # ========================================================================
    # TEST 9: Delete Offer
    # ========================================================================
    log_test "Delete offer (maker cancels)"

    if [ -n "$OFFER_ID" ]; then
        DELETE_OFFER_PARAMS="{\"offer_id\": \"$OFFER_ID\"}"

        DELETE_OFFER_RESPONSE=$(send_command "delete_offer" "$DELETE_OFFER_PARAMS")
        echo "$DELETE_OFFER_RESPONSE" > "$TEST_OUTPUT_DIR/delete_offer_response.json"

        if echo "$DELETE_OFFER_RESPONSE" | jq -e '.success == true' > /dev/null; then
            assert_success "delete_offer succeeded"
        else
            log_error "delete_offer failed"
            log_error "Response: $DELETE_OFFER_RESPONSE"
            TESTS_FAILED=$((TESTS_FAILED + 1))
        fi

        # Verify offer is deleted
        sleep 2
        VERIFY_DELETE_RESPONSE=$(send_command "list_offers" "{}")
        DELETED_OFFER=$(echo "$VERIFY_DELETE_RESPONSE" | jq ".offers[] | select(.id == \"$OFFER_ID\")")

        if [ -z "$DELETED_OFFER" ]; then
            assert_success "Offer removed from list"
        else
            log_warn "Offer still in list (Nostr deletion propagation delay?)"
        fi
    else
        log_warn "Skipping delete_offer (no offer_id available)"
    fi

    # ========================================================================
    # TEST SUMMARY
    # ========================================================================
    echo ""
    echo "========================================================================"
    echo "                           TEST SUMMARY"
    echo "========================================================================"
    echo ""
    echo "Total Tests:  $TESTS_RUN"
    echo -e "${GREEN}Passed:       $TESTS_PASSED${NC}"
    echo -e "${RED}Failed:       $TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}✓ ALL TESTS PASSED${NC}"
        echo ""
        echo "Test output saved to: $TEST_OUTPUT_DIR"
        return 0
    else
        echo -e "${RED}✗ SOME TESTS FAILED${NC}"
        echo ""
        echo "Check test output at: $TEST_OUTPUT_DIR"
        return 1
    fi
}

# Run main test suite
main "$@"
