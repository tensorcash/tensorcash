#!/usr/bin/env bash
set -euo pipefail

# Simple test runner with coverage threshold

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "Running pytest with coverage (fail-under=80)..."

if python3 -c "import pytest, fastapi, httpx" >/dev/null 2>&1; then
  :
else
  echo "Missing dependencies. Please install dev requirements, e.g.:" >&2
  echo "  pip install pytest fastapi httpx requests pytest-cov" >&2
  exit 2
fi

if python3 -c "import pytest_cov" >/dev/null 2>&1; then
  pytest --cov=src --cov-report=term --cov-fail-under=80
else
  echo "pytest-cov not available; running tests without coverage." >&2
  echo "Install pytest-cov to enforce coverage threshold." >&2
  pytest
fi

