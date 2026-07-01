#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Verification-API test runner

set -euo pipefail

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$TEST_DIR/test_results"
mkdir -p "$RESULTS_DIR"

echo "========================================="
echo "Verification-API Test Suite"
echo "========================================="

# Resolve python (prefer 3.11, then 3.10)
if [ -n "${PYEXE:-}" ]; then
  if ! command -v "$PYEXE" >/dev/null 2>&1; then
    echo -e "${RED}Specified PYEXE='$PYEXE' not found in PATH${NC}"
    exit 1
  fi
else
  for CAND in python3.11 python3.10 python3 python; do
    if command -v "$CAND" >/dev/null 2>&1; then
      PYEXE="$CAND"
      break
    fi
  done
  if [ -z "${PYEXE:-}" ]; then
    echo -e "${RED}No suitable Python interpreter found (tried python3.11, 3.10, 3, python)${NC}"
    exit 1
  fi
fi
echo "Using Python interpreter: $PYEXE ($($PYEXE -V 2>/dev/null))"

# Prefer a local virtualenv for isolation; allow override via VENV_DIR
SERVICE_DIR="$ROOT_DIR/services/verification-api"
VENV_DIR="${VENV_DIR:-$SERVICE_DIR/.venv311}"

create_or_recreate_venv() {
  if [ -d "$VENV_DIR" ]; then
    if [ "${REBUILD_VENV:-0}" = "1" ]; then
      echo -e "${YELLOW}REBUILD_VENV=1 set; removing existing venv at $VENV_DIR${NC}"
      rm -rf "$VENV_DIR"
    else
      return 0
    fi
  fi
  echo -e "${YELLOW}Creating virtual environment at $VENV_DIR using $PYEXE${NC}"
  "$PYEXE" -m venv "$VENV_DIR"
}

ensure_pip_in_venv() {
  local vp="$VENV_DIR/bin/python"
  if ! "$vp" -m pip --version >/dev/null 2>&1; then
    echo -e "${YELLOW}Bootstrapping pip in venv (ensurepip)${NC}"
    if ! "$vp" -m ensurepip --upgrade >/dev/null 2>&1; then
      echo -e "${RED}Failed to bootstrap pip via ensurepip. Please install python3.11-venv or recreate Python with ensurepip.${NC}"
      return 1
    fi
  fi
  return 0
}

# Minimal test deps; runtime deps are mocked by tests/conftest.py
ensure_test_deps() {
  if [ "${SKIP_DEPS:-}" = "1" ]; then
    echo -e "${YELLOW}SKIP_DEPS=1 set; skipping dependency checks/install.${NC}"
    return 0
  fi
  echo -e "${YELLOW}Checking Python test dependencies...${NC}"
  
  # Always use venv for isolation and to avoid system pip issues
  create_or_recreate_venv
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo -e "${RED}Failed to create venv at $VENV_DIR${NC}"
    exit 2
  fi
  if ! ensure_pip_in_venv; then
    exit 2
  fi
  PYEXE="$VENV_DIR/bin/python"
  echo "Using venv Python: $PYEXE ($($PYEXE -V 2>/dev/null))"
  if ! "$PYEXE" - <<'PY'
import sys
import importlib
mods = [
  'pytest','pytest_asyncio','pytest_cov','pytest_timeout','pytest_mock','zmq','flatbuffers','numpy'
]
missing = []
for m in mods:
    try: importlib.import_module(m)
    except Exception: missing.append(m)
sys.exit(0 if not missing else 1)
PY
  then
    echo "Installing test dependencies..."
    "$PYEXE" -m pip install -q -U pip \
      pytest pytest-asyncio pytest-cov pytest-timeout pytest-mock \
      pyzmq flatbuffers numpy
    
    # Install all dependencies from requirements.txt for comprehensive testing
    echo "Installing full dependencies from requirements.txt..."
    "$PYEXE" -m pip install -r "$SERVICE_DIR/requirements.txt"
  fi
}

run_unit() {
  echo -e "${YELLOW}Running unit tests...${NC}"
  pushd "$TEST_DIR" >/dev/null
  export TEST_MODE=true
  export PYTHONDONTWRITEBYTECODE=1
  # Make src and schemas importable just in case
  export PYTHONPATH="$ROOT_DIR/services/verification-api/src:$ROOT_DIR/shared-utils/fb-schemas:${PYTHONPATH:-}"
  # Build optional flags based on available plugins when deps are skipped
  TIMEOUT_FLAGS=()
  if "$PYEXE" - <<'PY'
import importlib, sys
sys.exit(0 if importlib.util.find_spec("pytest_timeout") else 1)
PY
  then
    TIMEOUT_FLAGS+=(--timeout=60 --timeout-method=thread)
  fi
  COV_FLAGS=()
  if "$PYEXE" - <<'PY'
import importlib, sys
sys.exit(0 if importlib.util.find_spec("pytest_cov") else 1)
PY
  then
    COV_FLAGS+=(--cov=../src --cov-branch --cov-report=term-missing --cov-report=html:"$RESULTS_DIR/coverage")
    if [ -n "${COV_FAIL_UNDER:-}" ]; then COV_FLAGS+=(--cov-fail-under=${COV_FAIL_UNDER}); fi
  fi
  "$PYEXE" -m pytest -v unit --junit-xml="$RESULTS_DIR/unit_tests.xml" "${TIMEOUT_FLAGS[@]}" "${COV_FLAGS[@]}"
  popd >/dev/null
  echo -e "${GREEN}✓ Unit tests passed${NC}"
}

run_e2e() {
  echo -e "${YELLOW}Running e2e tests...${NC}"
  pushd "$TEST_DIR" >/dev/null
  export TEST_MODE=true
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPATH="$ROOT_DIR/services/verification-api/src:$ROOT_DIR/shared-utils/fb-schemas:${PYTHONPATH:-}"
  TIMEOUT_FLAGS=()
  if "$PYEXE" - <<'PY'
import importlib, sys
sys.exit(0 if importlib.util.find_spec("pytest_timeout") else 1)
PY
  then
    TIMEOUT_FLAGS+=(--timeout=120 --timeout-method=thread)
  fi
  "$PYEXE" -m pytest -v e2e --junit-xml="$RESULTS_DIR/e2e_tests.xml" "${TIMEOUT_FLAGS[@]}"
  popd >/dev/null
  echo -e "${GREEN}✓ E2E tests passed${NC}"
}

usage() {
  echo "Usage: $0 [unit|e2e|all]"
}

main() {
  ensure_test_deps

  case "${1:-all}" in
    unit)
      run_unit
      ;;
    e2e)
      run_e2e
      ;;
    all)
      run_unit
      run_e2e
      ;;
    *)
      usage
      exit 1
      ;;
  esac

  echo ""
  echo "Results in: $RESULTS_DIR"
  echo "  - Unit JUnit: $RESULTS_DIR/unit_tests.xml"
  echo "  - E2E JUnit:  $RESULTS_DIR/e2e_tests.xml"
  echo "  - Coverage:   $RESULTS_DIR/coverage/index.html"
}

main "$@"
