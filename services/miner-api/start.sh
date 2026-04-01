#!/usr/bin/env bash
set -euo pipefail

: "${VLLM_ENABLE_POW:=1}"
export VLLM_ENABLE_POW

exec python3 /app/vllm_supervisor.py
