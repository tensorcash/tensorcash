# SPDX-License-Identifier: Apache-2.0
"""Tests for the operator HTTP server bind/key startup guard.

An unset OPERATOR_API_KEY disables operator auth entirely
(OperatorHTTPHandler._check_auth), so the server must refuse to start on a
non-loopback bind without a key.
"""
import importlib
import os
import sys
import types

import pytest
from unittest.mock import patch


def _ensure_response_value_stub():
    """main.py needs proof.ResponseValue at import time; the generated
    ``proof`` package only exists in the Docker test image. Install a minimal
    stub when it is absent so this module also runs outside the container.
    """
    try:
        import proof.ResponseValue  # noqa: F401 -- generated code or conftest mock
    except Exception:
        pkg = sys.modules.get("proof") or types.ModuleType("proof")
        if not hasattr(pkg, "__path__"):
            pkg.__path__ = []
        sys.modules["proof"] = pkg
        rv_mod = types.ModuleType("proof.ResponseValue")

        class ResponseValue:
            Quick_OK = 1
            Quick_Fail = 2
            Quick_OK_Smell_OK = 3
            Quick_OK_Smell_Fail = 4
            Quick_Fail_Smell_Fail = 5
            Full_Green = 6
            Full_Amber = 7
            Full_Red = 8
            Model_OK = 9
            Model_Fail = 10

        rv_mod.ResponseValue = ResponseValue
        sys.modules["proof.ResponseValue"] = rv_mod
    # utils.proof may have been imported before proof.ResponseValue existed
    # (conftest installs its mock after importing utils.proof) and cached
    # ResponseValue=None; re-run the shim so main can import it.
    if "utils.proof" in sys.modules and getattr(sys.modules["utils.proof"], "ResponseValue", None) is None:
        importlib.reload(sys.modules["utils.proof"])


_ensure_response_value_stub()

main = pytest.importorskip("main")


class TestIsLoopbackAddr:
    def test_loopback_addresses(self):
        for addr in ("127.0.0.1", "127.0.0.53", "::1", "localhost"):
            assert main._is_loopback_addr(addr), addr

    def test_non_loopback_addresses(self):
        # Empty string means "all interfaces" to HTTPServer; hostnames are
        # unresolvable here and must be treated as exposed.
        for addr in ("0.0.0.0", "::", "", "203.0.113.5", "myhost.example"):
            assert not main._is_loopback_addr(addr), addr


class TestOperatorServerBindGuard:
    def _start(self):
        return main._start_operator_http_server(validator=None)

    def test_refuses_non_loopback_bind_without_key(self):
        env = {"OPERATOR_HTTP_BIND": "0.0.0.0", "OPERATOR_API_KEY": "", "OPERATOR_HTTP_PORT": "0"}
        with patch.dict(os.environ, env):
            with pytest.raises(SystemExit):
                self._start()

    def test_allows_loopback_bind_without_key(self):
        # Mock the server/thread: the unit under test is the guard, and the
        # suite's conftest stubs the socket module so nothing can really bind.
        env = {"OPERATOR_HTTP_BIND": "127.0.0.1", "OPERATOR_API_KEY": "", "OPERATOR_HTTP_PORT": "0"}
        with patch.dict(os.environ, env), \
             patch.object(main, "HTTPServer") as server_cls, \
             patch.object(main.threading, "Thread") as thread_cls:
            self._start()
            server_cls.assert_called_once_with(("127.0.0.1", 0), main.OperatorHTTPHandler)
            thread_cls.return_value.start.assert_called_once()

    def test_allows_non_loopback_bind_with_key(self):
        env = {"OPERATOR_HTTP_BIND": "0.0.0.0", "OPERATOR_API_KEY": "secret", "OPERATOR_HTTP_PORT": "0"}
        with patch.dict(os.environ, env), \
             patch.object(main, "HTTPServer") as server_cls, \
             patch.object(main.threading, "Thread") as thread_cls:
            self._start()
            server_cls.assert_called_once_with(("0.0.0.0", 0), main.OperatorHTTPHandler)
            thread_cls.return_value.start.assert_called_once()
