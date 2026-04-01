import os
import sys
import importlib
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
FIXTURES = Path(__file__).parent / "test_data.json"
ENV_KEYS = [
    "TEST_MODE", "REQUIRE_AUTH", "API_KEY", "RATE_LIMIT", "TEST_DATA_FILE",
    "ENABLE_TLS", "TLS_CERT", "TLS_KEY", "ALLOWED_ORIGINS", "RPC_USER",
    "RPC_PASS", "COOKIE_FILE", "RPC_HOST", "RPC_PORT", "API_PORT",
    "LOG_LEVEL",
]


def load_app(env_overrides):
    # Ensure we import the module fresh with a clean environment for app settings
    for k in ENV_KEYS:
        if k in os.environ and k not in env_overrides:
            del os.environ[k]
    for k, v in env_overrides.items():
        os.environ[k] = v

    # Put src/ on sys.path so we can import api_server
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

    if "api_server" in sys.modules:
        del sys.modules["api_server"]

    mod = importlib.import_module("api_server")
    importlib.reload(mod)
    return mod.app


def test_root_and_health():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": str(FIXTURES),
        "LOG_LEVEL": "DEBUG",
    })
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "Bitcoin Core Model API"
        assert data["test_mode"] is True

        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


def test_models_auth_required_and_rate_limit():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "true",
        "API_KEY": "k1,k2",
        "RATE_LIMIT": "2",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        # Missing auth
        r = client.get("/api/v1/models")
        assert r.status_code == 403

        headers = {"Authorization": "Bearer k1"}
        # Within limit
        assert client.get("/api/v1/models", headers=headers).status_code == 200
        assert client.get("/api/v1/models", headers=headers).status_code == 200

        # Exceed limit
        r = client.get("/api/v1/models", headers=headers)
        assert r.status_code == 429

        # Different key works
        assert client.get("/api/v1/models", headers={"Authorization": "Bearer k2"}).status_code == 200


def test_models_list_variants():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "true",
        "API_KEY": "k1",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer k1"}
        r = client.get("/api/v1/models", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert {"model_hash", "model_name", "model_commit", "difficulty"}.issubset(data[0].keys())

        r = client.get("/api/v1/models?extended=true", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert {"cid", "txid", "block_hash", "block_height"}.issubset(data[0].keys())


def test_model_info_validation_and_parsing():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        # Invalid length
        assert client.get("/api/v1/models/abc").status_code == 400

        # Invalid characters
        bad = "z" * 64
        assert client.get(f"/api/v1/models/{bad}").status_code == 400

        # Valid present in fixtures
        good = "0" * 64
        r = client.get(f"/api/v1/models/{good}")
        assert r.status_code == 200
        body = r.json()
        assert body["model_hash"] == good
        assert "model_name" in body and "model_commit" in body


def test_docs_available_in_test_mode():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200


def test_tls_missing_cert_raises():
    # TLS enabled but files missing should raise at startup
    with pytest.raises(FileNotFoundError):
        app = load_app({
            "TEST_MODE": "true",
            "REQUIRE_AUTH": "false",
            "ENABLE_TLS": "true",
            "TLS_CERT": "/nope/cert.pem",
            "TLS_KEY": "/nope/key.pem",
            "TEST_DATA_FILE": str(FIXTURES),
        })
        # Entering the client triggers lifespan startup
        with TestClient(app):
            pass


def test_auth_disabled_allows_access():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        assert client.get("/api/v1/models").status_code == 200
        good = "1234567890abcdef" * 4
        assert client.get(f"/api/v1/models/{good}").status_code == 200


def test_invalid_api_key_rejected():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "true",
        "API_KEY": "valid-key",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        # Wrong key
        r = client.get("/api/v1/models", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 403


def test_model_not_found_message_passthrough():
    app = load_app({
        "TEST_MODE": "true",
        "REQUIRE_AUTH": "false",
        "TEST_DATA_FILE": str(FIXTURES),
    })
    with TestClient(app) as client:
        missing = "f" * 64
        r = client.get(f"/api/v1/models/{missing}")
        assert r.status_code == 200
        assert isinstance(r.json(), str)


def test_docs_disabled_when_not_in_test_mode():
    app = load_app({
        "TEST_MODE": "false",
        "REQUIRE_AUTH": "false",
        "RPC_USER": "user",
        "RPC_PASS": "pass",
        # Ensure cookie file not consulted during import validation
    })
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_health_reports_unhealthy_when_rpc_unavailable():
    app = load_app({
        "TEST_MODE": "false",
        "REQUIRE_AUTH": "false",
        "RPC_USER": "user",
        "RPC_PASS": "pass",
        # Missing cookie file will cause call_rpc to error
    })
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "unhealthy"
        assert "error" in data
