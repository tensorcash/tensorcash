#!/bin/bash
# Copyright (c) 2025 The TensorCash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://opensource.org/license/mit/.

# Simplified E2E Test Script for Bulletin Board Trading System
# Uses stdin batch mode to send all commands at once

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_BIN="${SCRIPT_DIR}/../target/debug/cosign-bridge"
TEST_OUTPUT_DIR="${SCRIPT_DIR}/../target/e2e_test_output"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

main() {
    log_info "Starting Simplified E2E Bulletin Board Tests"
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

    log_info "Running sequence of commands..."

    # Send all commands via stdin and collect responses
    {
        # TEST 1: init_bb
        echo '{"command":"init_bb","params":{}}'

        # Give time for initialization
        sleep 2

        # TEST 2: post_offer
        echo '{"command":"post_offer","params":{"offer_type":"sell","asset_send":"BTC","asset_recv":"USD","amount":0.1,"price":65000.0,"payment_methods":["bank_transfer","cash"],"regions":["US","EU"],"requires_escrow":true,"min_reputation_score":50.0}}'

        # Give time for Nostr propagation
        sleep 3

        # TEST 3: list_offers
        echo '{"command":"list_offers","params":{}}'

        # TEST 4: list_requests
        echo '{"command":"list_requests","params":{}}'

    } | timeout 60s "$BRIDGE_BIN" 2>"$TEST_OUTPUT_DIR/bridge_stderr.log" | tee "$TEST_OUTPUT_DIR/responses.jsonl"

    log_info "Responses saved to: $TEST_OUTPUT_DIR/responses.jsonl"
    log_info "Bridge logs saved to: $TEST_OUTPUT_DIR/bridge_stderr.log"

    # Parse and validate responses
    log_info "Parsing responses..."

    local line_num=0
    while IFS= read -r line; do
        line_num=$((line_num + 1))
        echo "$line" > "$TEST_OUTPUT_DIR/response_${line_num}.json"

        if echo "$line" | jq -e '.success == true' > /dev/null 2>&1; then
            log_info "✓ Response $line_num: success"
        elif echo "$line" | jq -e '.error' > /dev/null 2>&1; then
            local error=$(echo "$line" | jq -r '.error')
            log_error "✗ Response $line_num: $error"
        else
            log_info "Response $line_num: $(echo "$line" | jq -c '.')"
        fi
    done < "$TEST_OUTPUT_DIR/responses.jsonl"

    # Check specific responses
    log_info ""
    log_info "=== Validation ==="

    # Response 1: init_bb
    if [ -f "$TEST_OUTPUT_DIR/response_1.json" ]; then
        local pubkey=$(jq -r '.pubkey' "$TEST_OUTPUT_DIR/response_1.json")
        if [ -n "$pubkey" ] && [ "$pubkey" != "null" ]; then
            log_info "✓ init_bb: pubkey = $pubkey"
        else
            log_error "✗ init_bb: no pubkey"
        fi
    fi

    # Response 2: post_offer
    if [ -f "$TEST_OUTPUT_DIR/response_2.json" ]; then
        local offer_id=$(jq -r '.offer_id' "$TEST_OUTPUT_DIR/response_2.json" 2>/dev/null)
        if [ -n "$offer_id" ] && [ "$offer_id" != "null" ]; then
            log_info "✓ post_offer: offer_id = $offer_id"
        else
            log_error "✗ post_offer: no offer_id"
        fi
    fi

    # Response 3: list_offers
    if [ -f "$TEST_OUTPUT_DIR/response_3.json" ]; then
        local offer_count=$(jq '.offers | length' "$TEST_OUTPUT_DIR/response_3.json" 2>/dev/null)
        if [ -n "$offer_count" ]; then
            log_info "✓ list_offers: found $offer_count offers"
        else
            log_error "✗ list_offers: invalid response"
        fi
    fi

    log_info ""
    log_info "Test complete. Check $TEST_OUTPUT_DIR for detailed results."
}

main "$@"
