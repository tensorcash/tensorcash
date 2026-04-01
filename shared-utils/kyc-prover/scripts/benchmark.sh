#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Benchmark proof generation performance

set -e

cd "$(dirname "$0")/.."

BASE_URL="${1:-http://localhost:8080}"
ITERATIONS="${2:-3}"
MAX_MS="${3:-10000}"

echo "Benchmarking proof generation (${ITERATIONS} iterations)"
echo "Max allowed: ${MAX_MS}ms"
echo ""

TIMES=()

for i in $(seq 1 $ITERATIONS); do
  echo "► Run $i/$ITERATIONS..."

  START_MS=$(($(date +%s%N) / 1000000))

  # Run proof generation (suppress output)
  ./scripts/test_proof.sh > /dev/null 2>&1

  END_MS=$(($(date +%s%N) / 1000000))
  DURATION=$((END_MS - START_MS))

  TIMES+=($DURATION)
  echo "  Time: ${DURATION}ms"
done

echo ""

# Calculate statistics
TOTAL=0
MIN=${TIMES[0]}
MAX=${TIMES[0]}

for t in "${TIMES[@]}"; do
  TOTAL=$((TOTAL + t))
  if [ $t -lt $MIN ]; then MIN=$t; fi
  if [ $t -gt $MAX ]; then MAX=$t; fi
done

AVG=$((TOTAL / ITERATIONS))

echo "═══════════════════════════════════════════════════════════"
echo "  Performance Results"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Min:     ${MIN}ms"
echo "Max:     ${MAX}ms"
echo "Average: ${AVG}ms"
echo ""

# Check performance threshold
if [ $AVG -gt $MAX_MS ]; then
  echo "✗ Performance regression detected!"
  echo "  Average (${AVG}ms) exceeds threshold (${MAX_MS}ms)"
  exit 1
fi

echo "✓ Performance within acceptable range"
