"""Unit tests for onion-claim-agent.py (pool claim / install / rotate logic).

The agent is a hyphenated-name daemon script, so it is loaded via importlib.
All k8s / bitcoin-cli / Tor interactions are monkeypatched; the filesystem
paths are pointed into a per-test tmp sandbox.
"""
import base64
import importlib.util
import json
import os
import ssl
import urllib.error
from pathlib import Path

import pytest

AGENT_FILE = Path(__file__).resolve().parents[1] / "onion-claim-agent.py"

KEY_HEADER = b"== ed25519v1-secret: type0 =="


def _default_cafile():
    for p in (ssl.get_default_verify_paths().cafile,
              "/etc/ssl/certs/ca-certificates.crt",
              "/etc/ssl/cert.pem"):
        if p and os.path.exists(p):
            return p
    pytest.skip("no system CA bundle available")


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("K8S_TOKEN", "test-token")
    monkeypatch.setenv("K8S_CA", _default_cafile())
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.test")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    spec = importlib.util.spec_from_file_location("onion_claim_agent", AGENT_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hs = tmp_path / "hs"
    data = tmp_path / "data"
    data.mkdir()
    mod.HS_DIR = str(hs)
    mod.DATA_DIR = str(data)
    mod.CONF = str(data / "bitcoin.conf")
    mod.EXPIRY_FILE = str(hs / ".expiry_height")
    mod.WANT_PREFIX = ""
    return mod


def _bundle(name, expiry, prefix="tensorc", claimed="false", rv="1"):
    return {
        "metadata": {
            "name": name,
            "resourceVersion": rv,
            "labels": {"onion-grinder/prefix": prefix, "onion-grinder/claimed": claimed},
            "annotations": {"onion-grinder/expiry_height": str(expiry)},
        },
        "data": {},
    }


# ---------------- need_refresh / read_current_expiry ----------------

def test_need_refresh_no_onion(agent):
    refresh, why = agent.need_refresh(tip=1000)
    assert refresh and "no onion" in why


def test_need_refresh_no_expiry_marker(agent):
    os.makedirs(agent.HS_DIR)
    Path(agent.HS_DIR, "hostname").write_text("abc.onion\n")
    refresh, why = agent.need_refresh(tip=1000)
    assert refresh and "unknown expiry" in why


def test_need_refresh_about_to_expire(agent):
    os.makedirs(agent.HS_DIR)
    Path(agent.HS_DIR, "hostname").write_text("abc.onion\n")
    Path(agent.EXPIRY_FILE).write_text("1100")
    refresh, why = agent.need_refresh(tip=1000)  # 100 <= buffer(200)
    assert refresh and "about to expire" in why


def test_need_refresh_fresh(agent):
    os.makedirs(agent.HS_DIR)
    Path(agent.HS_DIR, "hostname").write_text("abc.onion\n")
    Path(agent.EXPIRY_FILE).write_text("2000")
    refresh, why = agent.need_refresh(tip=1000)  # 1000 blocks of runway
    assert not refresh and "fresh" in why


def test_read_current_expiry_garbage(agent):
    os.makedirs(agent.HS_DIR)
    Path(agent.EXPIRY_FILE).write_text("not-a-number")
    assert agent.read_current_expiry() is None
    Path(agent.EXPIRY_FILE).write_text(" 4321 ")
    assert agent.read_current_expiry() == 4321


# ---------------- get_tip / bitcoin_cli ----------------

def test_get_tip(agent, monkeypatch):
    monkeypatch.setattr(agent, "bitcoin_cli", lambda *a: (0, "1234", ""))
    assert agent.get_tip() == 1234
    monkeypatch.setattr(agent, "bitcoin_cli", lambda *a: (1, "", "connection refused"))
    assert agent.get_tip() is None
    monkeypatch.setattr(agent, "bitcoin_cli", lambda *a: (0, "flurb", ""))
    assert agent.get_tip() is None


# ---------------- k8s helper ----------------

def test_k8s_get_and_post(agent, monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return b'{"items": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, context=None, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    st, body = agent.k8s("GET", "/api/v1/namespaces/onion-grinder/secrets")
    assert st == 200 and body == {"items": []}
    assert captured["auth"] == "Bearer test-token"
    assert captured["body"] is None

    st, _ = agent.k8s("PUT", "/api/v1/x", body={"a": 1})
    assert st == 200
    assert json.loads(captured["body"]) == {"a": 1}


# ---------------- list_fresh_bundles ----------------

def test_list_fresh_bundles_filters_and_sorts(agent, monkeypatch):
    items = [
        _bundle("near-expiry", expiry=1150),   # 150 <= buffer(200): filtered
        _bundle("long-runway", expiry=9000),
        _bundle("mid-runway", expiry=5000),
        _bundle("wrong-prefix", expiry=8000, prefix="ten"),
    ]
    monkeypatch.setattr(agent, "k8s", lambda m, p, body=None: (200, {"items": items}))

    out = agent.list_fresh_bundles(tip=1000)
    names = [s["metadata"]["name"] for _, s in out]
    assert names == ["long-runway", "wrong-prefix", "mid-runway"]  # most runway first

    agent.WANT_PREFIX = "tensorc"
    out = agent.list_fresh_bundles(tip=1000)
    names = [s["metadata"]["name"] for _, s in out]
    assert names == ["long-runway", "mid-runway"]


# ---------------- claim ----------------

def test_claim_success_marks_bundle(agent, monkeypatch):
    sent = {}

    def fake_k8s(method, path, body=None):
        sent["method"], sent["path"], sent["body"] = method, path, body
        return 200, {}

    monkeypatch.setattr(agent, "k8s", fake_k8s)
    secret = _bundle("b1", expiry=9000)
    assert agent.claim(secret) is True
    assert sent["method"] == "PUT" and sent["path"].endswith("/secrets/b1")
    assert sent["body"]["metadata"]["labels"]["onion-grinder/claimed"] == "true"
    assert sent["body"]["metadata"]["annotations"]["onion-grinder/claimed_by"] == agent.NODE_ID


def test_claim_409_lost_race(agent, monkeypatch):
    def fake_k8s(method, path, body=None):
        raise urllib.error.HTTPError(path, 409, "conflict", None, None)

    monkeypatch.setattr(agent, "k8s", fake_k8s)
    assert agent.claim(_bundle("b1", expiry=9000)) is False


def test_claim_other_http_error_raises(agent, monkeypatch):
    def fake_k8s(method, path, body=None):
        raise urllib.error.HTTPError(path, 500, "boom", None, None)

    monkeypatch.setattr(agent, "k8s", fake_k8s)
    with pytest.raises(urllib.error.HTTPError):
        agent.claim(_bundle("b1", expiry=9000))


# ---------------- set_externalip ----------------

def test_set_externalip_appends_when_absent(agent):
    Path(agent.CONF).write_text("rpcuser=x\n")
    agent.set_externalip("abc.onion")
    text = Path(agent.CONF).read_text()
    assert f"externalip=abc.onion:{agent.P2P_PORT}" in text
    assert "listenonion=0" in text
    assert text.startswith("rpcuser=x")


def test_set_externalip_replaces_existing(agent):
    Path(agent.CONF).write_text("externalip=old.onion:1\nlistenonion=1\nrpcuser=x\n")
    agent.set_externalip("new.onion")
    lines = Path(agent.CONF).read_text().splitlines()
    assert f"externalip=new.onion:{agent.P2P_PORT}" in lines
    assert "listenonion=0" in lines
    assert "externalip=old.onion:1" not in lines
    assert "listenonion=1" not in lines


# ---------------- restart_bitcoind_if_quiescent ----------------

def test_restart_when_quiescent(agent, monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "bitcoin_cli",
                        lambda *a: (0, json.dumps({"blocks": 500, "headers": 500}), ""))
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or type("R", (), {"returncode": 0})())
    assert agent.restart_bitcoind_if_quiescent() is True
    assert ["supervisorctl", "restart", "node"] in calls


def test_no_restart_when_syncing(agent, monkeypatch):
    monkeypatch.setattr(agent, "bitcoin_cli",
                        lambda *a: (0, json.dumps({"blocks": 400, "headers": 500}), ""))
    assert agent.restart_bitcoind_if_quiescent() is False


def test_no_restart_on_bad_rpc(agent, monkeypatch):
    monkeypatch.setattr(agent, "bitcoin_cli", lambda *a: (1, "", "error"))
    assert agent.restart_bitcoind_if_quiescent() is False


# ---------------- install ----------------

def test_install_writes_key_and_reads_hostname(agent, monkeypatch):
    body = KEY_HEADER + b"\x00" * (96 - len(KEY_HEADER))
    secret = {"data": {"hs_ed25519_secret_key": base64.b64encode(body).decode()}}

    def fake_run(argv, **kw):
        if argv[:3] == ["supervisorctl", "signal", "HUP"]:
            Path(agent.HS_DIR, "hostname").write_text("fresh.onion\n")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    onion = agent.install(secret)
    assert onion == "fresh.onion"
    key_path = Path(agent.HS_DIR, "hs_ed25519_secret_key")
    assert key_path.read_bytes() == body
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert not os.path.exists(agent.HS_DIR + ".staging")


def test_install_rejects_bad_key_blob(agent):
    secret = {"data": {"hs_ed25519_secret_key": base64.b64encode(b"garbage").decode()}}
    with pytest.raises(AssertionError):
        agent.install(secret)


# ---------------- rotate / tick / main ----------------

def test_rotate_claims_installs_and_records_expiry(agent, monkeypatch):
    os.makedirs(agent.HS_DIR)
    secret = _bundle("b1", expiry=9000)
    monkeypatch.setattr(agent, "list_fresh_bundles", lambda tip: [(9000, secret)])
    monkeypatch.setattr(agent, "claim", lambda s: True)
    monkeypatch.setattr(agent, "install", lambda s: "fresh.onion")
    conf_written = {}
    monkeypatch.setattr(agent, "set_externalip", lambda o: conf_written.update(onion=o))
    monkeypatch.setattr(agent, "restart_bitcoind_if_quiescent", lambda: False)

    assert agent.rotate(tip=1000) is True
    assert conf_written["onion"] == "fresh.onion"
    assert Path(agent.EXPIRY_FILE).read_text() == "9000"


def test_rotate_tries_next_bundle_on_lost_race(agent, monkeypatch):
    os.makedirs(agent.HS_DIR)
    b1, b2 = _bundle("b1", expiry=9000), _bundle("b2", expiry=8000)
    monkeypatch.setattr(agent, "list_fresh_bundles", lambda tip: [(9000, b1), (8000, b2)])
    monkeypatch.setattr(agent, "claim", lambda s: s["metadata"]["name"] == "b2")
    monkeypatch.setattr(agent, "install", lambda s: "second.onion")
    monkeypatch.setattr(agent, "set_externalip", lambda o: None)
    monkeypatch.setattr(agent, "restart_bitcoind_if_quiescent", lambda: False)

    assert agent.rotate(tip=1000) is True
    assert Path(agent.EXPIRY_FILE).read_text() == "8000"


def test_rotate_pool_empty(agent, monkeypatch):
    monkeypatch.setattr(agent, "list_fresh_bundles", lambda tip: [])
    assert agent.rotate(tip=1000) is False


def test_rotate_install_failure(agent, monkeypatch):
    monkeypatch.setattr(agent, "list_fresh_bundles", lambda tip: [(9000, _bundle("b1", 9000))])
    monkeypatch.setattr(agent, "claim", lambda s: True)
    monkeypatch.setattr(agent, "install", lambda s: None)
    assert agent.rotate(tip=1000) is False


def test_tick_rpc_not_ready(agent, monkeypatch):
    monkeypatch.setattr(agent, "get_tip", lambda: None)
    rotated = []
    monkeypatch.setattr(agent, "rotate", lambda tip: rotated.append(tip))
    agent.tick()
    assert rotated == []


def test_tick_rotates_when_refresh_needed(agent, monkeypatch):
    monkeypatch.setattr(agent, "get_tip", lambda: 1000)
    rotated = []
    monkeypatch.setattr(agent, "rotate", lambda tip: rotated.append(tip))
    agent.tick()  # sandbox has no onion installed -> refresh needed
    assert rotated == [1000]


def test_tick_no_refresh_when_fresh(agent, monkeypatch):
    os.makedirs(agent.HS_DIR)
    Path(agent.HS_DIR, "hostname").write_text("abc.onion\n")
    Path(agent.EXPIRY_FILE).write_text("9000")
    monkeypatch.setattr(agent, "get_tip", lambda: 1000)
    rotated = []
    monkeypatch.setattr(agent, "rotate", lambda tip: rotated.append(tip))
    agent.tick()
    assert rotated == []


def test_main_run_once_swallows_loop_error(agent, monkeypatch):
    monkeypatch.setenv("RUN_ONCE", "1")
    ticks = []

    def boom():
        ticks.append(1)
        raise RuntimeError("transient")

    monkeypatch.setattr(agent, "tick", boom)
    agent.main()  # must catch the error and exit after one iteration
    assert ticks == [1]
