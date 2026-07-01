#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Test that proof with wrong VK fails verification

set -e

cd "$(dirname "$0")/.."

BASE_URL="${1:-http://localhost:8080}"
VECTORS_FILE="vectors/golden_vectors.json"

echo "Testing cross-VK rejection at $BASE_URL"
echo ""

# Load valid proof from vector
PROOF=$(jq -r '.[] | select(.name == "valid") | .proof_hex' "$VECTORS_FILE")
INPUTS=$(jq -r '.[] | select(.name == "valid") | .public_inputs_hex' "$VECTORS_FILE")
CORRECT_VK=$(jq -r '.[] | select(.name == "valid") | .vk_gnark_hex' "$VECTORS_FILE") # Use gnark format for Go verification

echo "► Test 1: Verification with correct VK..."
RESULT=$(curl -s -X POST "$BASE_URL/verify" \
  -H "Content-Type: application/json" \
  -d "{\"proof_hex\":\"$PROOF\",\"public_inputs_hex\":\"$INPUTS\",\"vk_hex\":\"$CORRECT_VK\"}")

VALID=$(echo "$RESULT" | jq -r '.valid')
if [ "$VALID" != "true" ]; then
  echo "✗ Proof rejected with correct VK!"
  exit 1
fi
echo "✓ Proof accepted with correct VK"
echo ""

# Generate a different VK by running setup again
echo "► Generating alternate VK..."
mkdir -p .tmp
./gentest -output .tmp > /dev/null 2>&1

WRONG_VK=$(jq -r '.[] | select(.name == "valid") | .vk_gnark_hex' .tmp/golden_vectors.json) # Use gnark format

# Verify they're different
if [ "$CORRECT_VK" == "$WRONG_VK" ]; then
  echo "✗ Generated VK matches (should be different)!"
  rm -rf .tmp
  exit 1
fi
echo "✓ Generated alternate VK"
echo ""

# Try to verify with wrong VK
echo "► Test 2: Verification with wrong VK..."
RESULT=$(curl -s -X POST "$BASE_URL/verify" \
  -H "Content-Type: application/json" \
  -d "{\"proof_hex\":\"$PROOF\",\"public_inputs_hex\":\"$INPUTS\",\"vk_hex\":\"$WRONG_VK\"}")

VALID=$(echo "$RESULT" | jq -r '.valid')
if [ "$VALID" == "true" ]; then
  echo "✗ Proof accepted with wrong VK!"
  rm -rf .tmp
  exit 1
fi
echo "✓ Proof rejected with wrong VK"
echo ""

# Cleanup
rm -rf .tmp

echo "═══════════════════════════════════════════════════════════"
echo "  Cross-VK test passed!"
echo "═══════════════════════════════════════════════════════════"
