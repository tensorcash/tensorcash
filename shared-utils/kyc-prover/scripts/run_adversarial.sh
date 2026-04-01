#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run the HDv1 adversarial soundness battery in a stock golang container.
#
# The gnark fork (tensorcash/gnark v0.9.1-plain-rangecheck) is PUBLIC, so
# `go mod download` needs no credentials. test.IsSolved only runs the R1CS solver
# (no trusted setup, no proving keys), so this is self-contained.
#
# Usage:
#   scripts/run_adversarial.sh            # fast tests (fork + under-constraint)
#   scripts/run_adversarial.sh full       # everything incl. heavy full-circuit
#   scripts/run_adversarial.sh '<regex>'  # custom -run filter
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

case "${1:-fast}" in
  fast) RUN='TestForkRangeCheck|TestForkEmulated|TestUnderconstraint|TestHint' ;;
  full) RUN='TestForkRangeCheck|TestForkEmulated|TestUnderconstraint|TestHint|TestAdversarial' ;;
  *)    RUN="$1" ;;
esac

echo ">> running circuit tests matching: ${RUN}"
docker run --rm \
  -v "${REPO_ROOT}:/src" \
  -v kyc-adv-gomod:/go/pkg/mod \
  -v kyc-adv-gocache:/root/.cache/go-build \
  -w /src \
  -e GOFLAGS=-mod=mod \
  golang:1.21 \
  go test ./internal/circuit/ -run "${RUN}" -v -timeout 30m -count=1
