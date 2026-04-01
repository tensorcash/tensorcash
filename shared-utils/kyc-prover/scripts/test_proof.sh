#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Test the KYC prover service with a valid proof request

set -e

cd "$(dirname "$0")/.."

BASE_URL="${1:-http://localhost:8080}"

echo "Testing KYC Prover at $BASE_URL"
echo ""

# Check health
echo "► Health check..."
curl -s "$BASE_URL/health" | jq .
echo ""

# Check if golden vectors exist
VECTORS_FILE="vectors/golden_vectors.json"

if [ ! -f "$VECTORS_FILE" ]; then
    echo "✗ Golden vectors not found at $VECTORS_FILE"
    echo ""
    echo "Generating golden vectors now..."
    echo ""
    ./scripts/generate_vectors.sh
    echo ""
fi

# Load valid witness from golden vectors
echo "► Loading valid witness from golden vectors..."
VALID_WITNESS=$(jq -r '.[] | select(.name == "valid") | .witness' "$VECTORS_FILE")

if [ -z "$VALID_WITNESS" ] || [ "$VALID_WITNESS" == "null" ]; then
    echo "✗ Could not find 'valid' vector in $VECTORS_FILE"
    exit 1
fi

# Build request with valid witness
REQUEST=$(jq -n \
  --argjson witness "$VALID_WITNESS" \
  '{
    chain_separator: $witness.chain_separator,
    asset_id: $witness.asset_id,
    compliance_root: $witness.compliance_root,
    tfr_anchor: $witness.tfr_anchor,
    witness: {
      secret: $witness.secret,
      pubkey_hash: $witness.pubkey_hash,
      country: $witness.country,
      age: $witness.age,
      merkle_proof: $witness.merkle_proof,
      merkle_index: $witness.merkle_index,
      merkle_leaf_hash: $witness.merkle_leaf_hash
    }
  }')

echo "✓ Loaded valid witness"
echo ""

echo "► Generating proof with valid witness..."
RESPONSE=$(curl -s -X POST "$BASE_URL/prove" \
  -H "Content-Type: application/json" \
  -d "$REQUEST")

echo "$RESPONSE" | jq .

# Check for errors
ERROR=$(echo "$RESPONSE" | jq -r '.error // empty')
if [ -n "$ERROR" ]; then
    echo ""
    echo "✗ Proof generation failed with error:"
    echo "$ERROR"
    exit 1
fi

# Extract proof and inputs
PROOF_HEX=$(echo "$RESPONSE" | jq -r '.proof_hex')
INPUTS_HEX=$(echo "$RESPONSE" | jq -r '.public_inputs_hex')

if [ "$PROOF_HEX" != "null" ] && [ "$INPUTS_HEX" != "null" ] && [ -n "$PROOF_HEX" ]; then
    echo ""
    echo "✓ Proof generated successfully!"
    echo ""
    echo "Proof length: ${#PROOF_HEX} chars"
    echo "Inputs length: ${#INPUTS_HEX} chars"
    echo ""
    echo "Proof: ${PROOF_HEX:0:66}..."
    echo "Inputs: ${INPUTS_HEX:0:66}..."
else
    echo ""
    echo "✗ Proof generation failed - no proof returned"
    exit 1
fi
