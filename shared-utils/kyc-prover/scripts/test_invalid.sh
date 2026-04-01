#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Test that invalid witnesses fail proof generation

set -e

cd "$(dirname "$0")/.."

BASE_URL="${1:-http://localhost:8080}"
VECTORS_FILE="vectors/golden_vectors.json"

echo "Testing invalid witness rejection at $BASE_URL"
echo ""

# Test each invalid vector
for name in invalid_secret invalid_age invalid_country invalid_merkle; do
  echo "► Testing $name..."

  # Check if vector exists
  WITNESS=$(jq -r ".[] | select(.name == \"$name\") | .witness" "$VECTORS_FILE")

  if [ -z "$WITNESS" ] || [ "$WITNESS" == "null" ]; then
    echo "✗ Vector '$name' not found!"
    exit 1
  fi

  # Extract witness fields
  CHAIN_SEP=$(echo "$WITNESS" | jq -r '.chain_separator')
  ASSET_ID=$(echo "$WITNESS" | jq -r '.asset_id')
  COMP_ROOT=$(echo "$WITNESS" | jq -r '.compliance_root')
  TFR_ANCHOR=$(echo "$WITNESS" | jq -r '.tfr_anchor')

  # Build request
  REQUEST=$(jq -n \
    --argjson witness "$WITNESS" \
    --arg chain_sep "$CHAIN_SEP" \
    --arg asset_id "$ASSET_ID" \
    --arg comp_root "$COMP_ROOT" \
    --arg tfr_anchor "$TFR_ANCHOR" \
    '{
      chain_separator: $chain_sep,
      asset_id: $asset_id,
      compliance_root: $comp_root,
      tfr_anchor: $tfr_anchor,
      witness: $witness
    }')

  # Try to generate proof (should fail)
  RESULT=$(curl -s -X POST "$BASE_URL/prove" \
    -H "Content-Type: application/json" \
    -d "$REQUEST")

  SUCCESS=$(echo "$RESULT" | jq -r '.success // false')

  if [ "$SUCCESS" == "true" ]; then
    echo "✗ $name produced a valid proof (CIRCUIT BUG!)"
    echo "$RESULT" | jq .
    exit 1
  fi

  # Should have error
  ERROR=$(echo "$RESULT" | jq -r '.error // empty')
  if [ -z "$ERROR" ]; then
    echo "⚠ No error message returned"
  fi

  echo "✓ $name correctly failed"
  echo ""
done

echo "═══════════════════════════════════════════════════════════"
echo "  All invalid witness tests passed!"
echo "═══════════════════════════════════════════════════════════"
