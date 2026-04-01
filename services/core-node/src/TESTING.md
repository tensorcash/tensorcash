Test Strategy for Core-Node API

Scope
- Covers functionality implemented under `services/core-node/src`:
  - FastAPI app (`api_server.py`): endpoints, auth, rate limiting, mode flags, and RPC adapter behavior in TEST_MODE.
  - Runtime configs are validated only insofar as they affect the API lifecycle (e.g., TLS checks). The Bitcoin Core fork in `bcore/` is out of scope and mocked via fixtures.

Test Setup
- Framework: `pytest` + `pytest-asyncio` + `httpx` AsyncClient (ASGI mode) or FastAPI `TestClient`.
- Import model reads environment at module import time. Use fixtures to set env and re-import the module per test context.
- Use TEST_MODE with the included fixtures file `src/tests/test_data.json` to avoid network/RPC.

Key Fixtures
- `env(monkeypatch, tmp_path)`:
  - Sets:
    - `TEST_MODE=true`
    - `REQUIRE_AUTH=true` or `false` (parametrized as needed)
    - `API_KEY="test-key-1,test-key-2"`
    - `RATE_LIMIT=2` (low for throttling tests)
    - `TEST_DATA_FILE=<absolute path to src/tests/test_data.json>`
    - `LOG_LEVEL=DEBUG`
  - Optionally sets TLS variables for TLS tests.
- `app(env)`:
  - Reloads `src.api_server` after env is set and returns `app`.
- `async_client(app)`:
  - Returns an `httpx.AsyncClient(app=app, base_url="http://test")` with lifespan context enabled.

Test Matrix
1) Public endpoints
   - GET `/` returns service metadata; `auth_required` field reflects `REQUIRE_AUTH`.
   - GET `/health` returns `{status: "healthy"}` and blockchain info from fixtures; no auth required.

2) Auth behavior
   - With `REQUIRE_AUTH=false`: accessing `/api/v1/models` and `/api/v1/models/{hash}` succeeds without Authorization header.
   - With `REQUIRE_AUTH=true` and no header: `/api/v1/models` → 403; `/api/v1/models/{hash}` → 403.
   - With invalid API key: 403 and no key leakage in logs/body.
   - With valid API key from `API_KEY`: 200 responses.

3) Rate limiting
   - Set `RATE_LIMIT=2`. Perform 2 requests with the same key → OK; 3rd request within a minute → 429.
   - Use a different key for subsequent requests → counter isolated (should succeed).
   - Optional: mock `time.time` to simulate window reset and assert requests succeed after a minute.

4) Models list
   - GET `/api/v1/models` (default `extended=false`): returns `getmodelslist_simple` fixtures; assert fields present and types (e.g., `difficulty` int).
   - GET `/api/v1/models?extended=true`: returns `getmodelslist_extended`; assert extended fields (`cid`, `txid`, `block_hash`, `block_height`).

5) Single model info
   - Valid 64‑char hex: returns parsed dict when fixture is `ModelRecord(...)` string; assert key mapping (`name`→`model_name`, `commit`→`model_commit`), numeric coercions, and `model_hash` injection.
   - Nonexistent model hash: returns string message from fixtures (ensure 200 and message body type).
   - Invalid length (≠64): 400.
   - Invalid characters: 400.

6) Docs availability
   - In `TEST_MODE=true`: `/docs` and `/redoc` return 200.
   - In `TEST_MODE=false`: `/docs` and `/redoc` return 404.

7) TLS checks (lifespan)
   - `ENABLE_TLS=true` with nonexistent `TLS_CERT`/`TLS_KEY`: app lifespan raises `FileNotFoundError` during startup.
   - (Optional) With temporary files created for cert/key paths: lifespan starts successfully. Note: this does not exercise real TLS sockets, only startup checks.

8) RPC adapter guardrails
   - In `TEST_MODE=true`: calling unsupported method via internal helper (if exposed) returns error payload. Through public API, verify only allowed methods are used.
   - (Optional) In `TEST_MODE=false`: ensure real RPC calls are not attempted within tests (skip or mark xfail); rely on TEST_MODE to isolate.

Example Test Skeleton (pytest)
```python
# tests/test_api.py
import os
import importlib
import json
import pytest
import httpx
from pathlib import Path


def reload_app_with_env(env_overrides):
    for k, v in env_overrides.items():
        os.environ[k] = v
    # Ensure fresh import of module-level constants
    mod = importlib.import_module("src.api_server")
    importlib.reload(mod)
    return mod.app


@pytest.fixture()
def fixtures_path():
    return str(Path(__file__).parent / "test_data.json")


@pytest.mark.asyncio
async def test_root_and_health(fixtures_path):
    app = reload_app_with_env({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": fixtures_path,
    })
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        r = await client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "Bitcoin Core Model API"

        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_models_auth_required_and_rate_limit(fixtures_path):
    app = reload_app_with_env({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "true",
        "API_KEY": "k1,k2",
        "RATE_LIMIT": "2",
        "TEST_DATA_FILE": fixtures_path,
    })
    headers = {"Authorization": "Bearer k1"}
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        # Missing auth
        r = await client.get("/api/v1/models")
        assert r.status_code == 403

        # Valid auth (within limit)
        assert (await client.get("/api/v1/models", headers=headers)).status_code == 200
        assert (await client.get("/api/v1/models", headers=headers)).status_code == 200

        # Over limit
        r = await client.get("/api/v1/models", headers=headers)
        assert r.status_code == 429

        # Different key has independent counter
        r = await client.get("/api/v1/models", headers={"Authorization": "Bearer k2"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_model_info_validation_and_parsing(fixtures_path):
    app = reload_app_with_env({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": fixtures_path,
    })
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        # Invalid length
        assert (await client.get("/api/v1/models/abc")).status_code == 400

        # Invalid characters
        bad = "z" * 64
        assert (await client.get(f"/api/v1/models/{bad}")).status_code == 400

        # Valid and present in fixtures
        good = "0" * 64
        r = await client.get(f"/api/v1/models/{good}")
        assert r.status_code == 200
        body = r.json()
        assert body["model_hash"] == good
        assert "model_name" in body and "model_commit" in body


@pytest.mark.asyncio
async def test_docs_available_in_test_mode(fixtures_path):
    app = reload_app_with_env({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": fixtures_path,
    })
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/redoc")).status_code == 200


def test_tls_missing_cert_raises(fixtures_path):
    # Lifespan should raise when TLS is enabled but certs are missing
    import pytest
    from fastapi.testclient import TestClient
    app = reload_app_with_env({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "ENABLE_TLS": "true",
        "TLS_CERT": "/nope/cert.pem",
        "TLS_KEY": "/nope/key.pem",
        "TEST_DATA_FILE": fixtures_path,
    })
    with pytest.raises(FileNotFoundError):
        # Entering context triggers lifespan startup
        TestClient(app)
```

Notes
- Tests avoid hitting real RPC by relying on TEST_MODE fixtures.
- Rate limiting is in‑memory and time‑based; tests set low limits to keep runtime short and may mock `time.time` for window resets.
- If desired, split tests into unit (helpers) vs API (endpoints) modules. The skeleton above exercises the public API surface end‑to‑end under TEST_MODE.

