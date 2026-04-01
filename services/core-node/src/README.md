TensorCash Core-Node: API and Runtime Overview

This document describes the functionality implemented in `services/core-node/src`. It focuses on the FastAPI service, runtime orchestration, and helper scripts that wrap a custom Bitcoin Core fork. The forked core implementation itself lives under `services/core-node/bcore` and is out of scope for this document.

Overview
- Purpose: Expose a minimal HTTP API to query model metadata recorded on-chain by the forked Bitcoin Core (“bcore”), and manage the node runtime (bitcoind, Tor, optional auto-mining).
- Scope: Everything under `src/` (FastAPI app, supervisor config, mining script, test fixtures). The `bcore/` directory is a Bitcoin Core fork with custom RPCs; its internals are not covered here.

Components
- `api_server.py`: FastAPI app exposing read-only endpoints backed by bcore JSON‑RPC.
- `supervisord.conf`: Orchestrates Tor, `bitcoind`, the API server, and optional auto‑mining.
- `start_mining.sh`: Convenience script to start mining to a provided wallet address.
- `tests/test_data.json`: Static fixtures that power TEST_MODE responses without hitting a live node.

API Server (`api_server.py`)
- Stack: FastAPI + Uvicorn, `httpx` (async) for RPC, Pydantic for typing.
- Authentication:
  - Header: `Authorization: Bearer <API_KEY>` when `REQUIRE_AUTH=true` (default).
  - Keys configured via `API_KEY` env var (comma‑separated). If `REQUIRE_AUTH=true` but no keys provided, a temporary key is generated and logged.
  - Rate limit: `RATE_LIMIT` requests/minute per API key (tracks hashed key). Default 60/min.
  - Endpoints `GET /` and `GET /health` are always public; model endpoints require auth if enabled.
- Transport Security:
  - TLS support via `ENABLE_TLS=true`, `TLS_CERT`, `TLS_KEY`. If enabled and certs are missing, startup fails.
  - CORS and Trusted Host middleware are present but currently commented out. If enabled, they would restrict origins and Host headers.
- RPC Backend:
  - Allowed RPC methods: `getmodelslist`, `getmodelinfo`, `getblockchaininfo`.
  - Auth uses the cookie file at `COOKIE_FILE` (preferred). Environment fallback is validated at startup but RPC requests read the cookie directly.
  - In `TEST_MODE=true`, all RPC calls return data from `tests/test_data.json`.
- Endpoints:
  - `GET /` → service metadata, flags, and listed endpoints.
  - `GET /health` → shallow blockchain info (`blocks`) or full info in test mode.
  - `GET /api/v1/models?extended=false` → list of models. When `extended=true`, returns richer records (cid, txid, block refs).
  - `GET /api/v1/models/{model_hash}` → details for a single model. Validates hex length (64) and characters. If bcore returns a `ModelRecord(...)` string, it is parsed and normalized (e.g., `name` → `model_name`, `commit` → `model_commit`).

Runtime Orchestration (`supervisord.conf`)
- `program:tor` → runs Tor for network privacy/connectivity.
- `program:bitcoind` → launches the forked daemon with `-datadir=/data -conf=/data/bitcoin.conf -printtoconsole`.
- `program:mining` → runs `./start_mining.sh` if `WALLET_ADDRESS` is provided.
- `program:api_server` → runs the FastAPI app (`python3 /app/api_server.py`). Propagates `MODEL_API_KEY` to `API_KEY` for auth.

Mining Helper (`start_mining.sh`)
- Waits 30 seconds for `bitcoind` to be ready; if `WALLET_ADDRESS` is set, executes:
  - `build/bin/bitcoin-cli -datadir=/data startmining "$WALLET_ADDRESS"`
- Note: `startmining` is a custom RPC provided by the bcore fork (not standard Bitcoin Core).

Test Fixtures (`tests/test_data.json`)
- Contains sample payloads for:
  - `getmodelslist` (simple and extended variants)
  - `getmodelinfo` (stringified `ModelRecord` entries)
  - `getblockchaininfo` (health check)
- Used when `TEST_MODE=true` to avoid live RPC calls.

Environment Variables
- Core RPC and API:
  - `RPC_HOST` (default `localhost`), `RPC_PORT` (default `18443`)
  - `COOKIE_FILE` (default `/data/caishtest/.cookie`) – preferred RPC auth
  - `RPC_USER`, `RPC_PASS` – validated at startup as fallback if cookie missing
  - `API_PORT` (default `8050`)
- Auth and limits:
  - `REQUIRE_AUTH` (default `true`), `API_KEY` (comma‑separated), `RATE_LIMIT` (default `60`)
- Security & network:
  - `ENABLE_TLS` (default `false`), `TLS_CERT` (default `/certs/cert.pem`), `TLS_KEY` (default `/certs/key.pem`)
  - `ALLOWED_ORIGINS` – used if CORS is enabled (currently commented out)
- Behavior & logging:
  - `TEST_MODE` (default `false`) – serve from fixtures
  - `LOG_LEVEL` (default `INFO`)
- Supervisor‑propagated:
  - `MODEL_API_KEY` → mapped to `API_KEY` for the API server
  - `WALLET_ADDRESS` → used by `start_mining.sh`

Operational Notes
- bcore fork: The `bcore/` directory contains a Bitcoin Core fork providing custom RPCs (e.g., `getmodelslist`, `getmodelinfo`, `startmining`). Its build system, consensus logic, and RPC internals are not covered here.
- Production hardening:
  - Consider enabling Trusted Host and CORS where appropriate.
  - Prefer cookie authentication for RPC.
  - Deploy behind a reverse proxy that terminates TLS or enable in‑app TLS with proper certificates.
  - Adjust rate limits per deployment needs.

Local Development
- Run with fixtures (no live node):
  1) `export TEST_MODE=true REQUIRE_AUTH=false LOG_LEVEL=DEBUG`
  2) `python3 src/api_server.py` (or `uvicorn src.api_server:app --reload --port 8050`)
- Example requests:
  - `curl http://localhost:8050/health`
  - `curl 'http://localhost:8050/api/v1/models?extended=true' -H 'Authorization: Bearer <API_KEY>'`
  - `curl http://localhost:8050/api/v1/models/<64-hex-hash> -H 'Authorization: Bearer <API_KEY>'`

Known Limitations
- Only a small subset of RPCs are exposed. RPC auth currently reads the cookie directly even when `RPC_USER/PASS` exist.
- Rate limiting is in‑memory and per‑process; it does not persist or coordinate across replicas.
- CORS and trusted host protections are present but disabled by default.

