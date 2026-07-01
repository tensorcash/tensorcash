#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Test proof verification with mutations

set -e

cd "$(dirname "$0")/.."

BASE_URL="${1:-http://localhost:8080}"
VECTORS_FILE="vectors/golden_vectors.json"

echo "Testing proof verification at $BASE_URL"
echo ""

# Load valid proof
PROOF=$(jq -r '.[] | select(.name == "valid") | .proof_hex' "$VECTORS_FILE")
INPUTS=$(jq -r '.[] | select(.name == "valid") | .public_inputs_hex' "$VECTORS_FILE")
VK=$(jq -r '.[] | select(.name == "valid") | .vk_gnark_hex' "$VECTORS_FILE") # Use gnark format for Go verification

# Test 1: Valid proof should verify
echo "► Test 1: Valid proof verification..."
RESULT=$(curl -s -X POST "$BASE_URL/verify" \
  -H "Content-Type: application/json" \
  -d "{\"proof_hex\":\"$PROOF\",\"public_inputs_hex\":\"$INPUTS\",\"vk_hex\":\"$VK\"}")

VALID=$(echo "$RESULT" | jq -r '.valid')
if [ "$VALID" != "true" ]; then
  echo "✗ Valid proof rejected!"
  echo "$RESULT" | jq .
  exit 1
fi
echo "✓ Valid proof accepted"
echo ""

# Test 2: Corrupted proof should fail
# Corrupt bytes deep inside the proof (past gnark's serialization header,
# into the actual G1/G2 curve point data) to ensure the pairing check fails.
# The header is ~52 bytes = ~104 hex chars; we corrupt at offset 120+ (well
# inside the Ar curve point) AND flip multiple bytes for robustness.
echo "► Test 2: Corrupted proof rejection..."
PROOF_LEN=${#PROOF}
MID=$((PROOF_LEN / 2))
CORRUPTED="${PROOF:0:$MID}DEADBEEF${PROOF:$((MID + 8))}"

RESULT=$(curl -s -X POST "$BASE_URL/verify" \
  -H "Content-Type: application/json" \
  -d "{\"proof_hex\":\"$CORRUPTED\",\"public_inputs_hex\":\"$INPUTS\",\"vk_hex\":\"$VK\"}")

VALID=$(echo "$RESULT" | jq -r '.valid')
if [ "$VALID" == "true" ]; then
  echo "✗ Corrupted proof accepted!"
  exit 1
fi
echo "✓ Corrupted proof rejected"
echo ""

# Test 3: Wrong public inputs should fail
echo "► Test 3: Wrong public inputs rejection..."
WRONG_INPUTS="${INPUTS:0:4}DEADBEEF${INPUTS:12}"

RESULT=$(curl -s -X POST "$BASE_URL/verify" \
  -H "Content-Type: application/json" \
  -d "{\"proof_hex\":\"$PROOF\",\"public_inputs_hex\":\"$WRONG_INPUTS\",\"vk_hex\":\"$VK\"}")

VALID=$(echo "$RESULT" | jq -r '.valid')
if [ "$VALID" == "true" ]; then
  echo "✗ Wrong inputs accepted!"
  exit 1
fi
echo "✓ Wrong inputs rejected"
echo ""

echo "═══════════════════════════════════════════════════════════"
echo "  All verification tests passed!"
echo "═══════════════════════════════════════════════════════════"
