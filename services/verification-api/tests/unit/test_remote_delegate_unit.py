# SPDX-License-Identifier: Apache-2.0
import io
import json

import pytest


def _fake_response(payload: dict):
    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(payload).encode("utf-8")
    return _Resp()


def test_verify_full_remote_maps_status(monkeypatch):
    from services.verification_api.src import remote_delegate as rd

    def _urlopen(req, timeout=None):
        assert req.get_header("Content-type") == "application/octet-stream"
        return _fake_response({"status": "Full_Green"})

    monkeypatch.setattr(rd._req, "urlopen", _urlopen)

    out = rd.verify_full_remote(b"bytes", base_url="https://example", api_key=None, timeout=1.0)
    from utils.proof import ResponseValue as RV
    assert out == RV.ResponseValue.Full_Green


def test_verify_model_remote_maps_status(monkeypatch):
    from services.verification_api.src import remote_delegate as rd

    def _urlopen(req, timeout):
        return _fake_response({"status": "Model_OK"})

    monkeypatch.setattr(rd._req, "urlopen", _urlopen)
    out = rd.verify_model_remote(b"bytes", base_url="https://example", api_key="k", timeout=1.0)
    from utils.proof import ResponseValue as RV
    assert out == RV.ResponseValue.Model_OK


def test_verify_full_remote_http_error(monkeypatch):
    from services.verification_api.src import remote_delegate as rd
    from urllib.error import HTTPError

    def _urlopen(req, timeout):
        raise HTTPError(req.full_url, 400, "bad", hdrs=None, fp=io.BytesIO(b"{\"error\":\"x\"}"))

    monkeypatch.setattr(rd._req, "urlopen", _urlopen)
    with pytest.raises(RuntimeError):
        rd.verify_full_remote(b"bytes", base_url="https://example", api_key=None, timeout=1.0)


def test_verify_full_remote_url_error(monkeypatch):
    from services.verification_api.src import remote_delegate as rd
    from urllib.error import URLError

    def _urlopen(req, timeout):
        raise URLError("offline")

    monkeypatch.setattr(rd._req, "urlopen", _urlopen)
    with pytest.raises(RuntimeError):
        rd.verify_full_remote(b"bytes", base_url="https://example", api_key=None, timeout=1.0)


def test_verify_full_remote_http_error_malformed_body(monkeypatch):
    import remote_delegate as rd
    from urllib.error import HTTPError
    import io

    def _urlopen(req, timeout=None):
        # Create HTTPError with malformed body that raises exception when read
        class BadResponse:
            def read(self):
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "invalid")
        
        error = HTTPError(req.full_url, 500, "Server Error", hdrs=None, fp=io.BytesIO(b"bad"))
        error.read = BadResponse().read
        raise error

    monkeypatch.setattr(rd._req, "urlopen", _urlopen)
    with pytest.raises(RuntimeError, match=r"HTTP 500:.*Server Error"):
        rd.verify_full_remote(b"bytes", base_url="https://example", api_key=None, timeout=1.0)


def test_map_status_unknown_raises():
    from services.verification_api.src.remote_delegate import _map_status_to_enum
    with pytest.raises(RuntimeError):
        _map_status_to_enum("NotAStatus")

