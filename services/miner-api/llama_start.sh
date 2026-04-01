#!/usr/bin/env bash
set -euo pipefail

echo "[Entrypoint] Starting llama supervisor"
exec python3 /app/llama_supervisor.py
