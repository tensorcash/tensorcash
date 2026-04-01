# SPDX-License-Identifier: Apache-2.0
"""POW_EGRESS_MODE env parsing + broker-mode safety checks.

These tests cover only ``MiningResponseWriter.__init__`` — the
env-driven config + validation surface introduced in slice 3 of
COMPUTE_BROKER_IMPROV.md §"PoW writer egress envvar contract".

They do NOT start the writer thread (no ZMQ sockets bound, no actual
network egress) so the suite is fast and side-effect-free.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

# The writer lives one dir up; mirror the sys.path setup the existing
# test_zmq_writer.py uses so any local subpackages resolve.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)


def _install_stub(name: str, attrs: tuple[str, ...] = ()) -> None:
    """Replace ``sys.modules[name]`` with a hollow stub exposing
    ``attrs``. The env-parsing tests don't exercise any of these
    modules, only the writer's ``__init__`` which reads ``os.environ``;
    the heavy deps appear only inside ``_serialize_response`` (never
    called) and ``_run`` (never started). The stubs guarantee
    ``zmq_pow_writer`` imports successfully regardless of which deps
    are present locally."""
    if name in sys.modules:
        del sys.modules[name]
    stub = types.ModuleType(name)
    for a in attrs:
        setattr(stub, a, type(a, (), {}))
    sys.modules[name] = stub


# Stub every heavy dep BEFORE importing zmq_pow_writer. The previous
# version of this file gated the import on a try/except → pytest.mark.
# skipif chain, which silently turned a missing dep into "20 skipped"
# in CI — a green test run that proved nothing. The hard import below
# means a regression of the parse/validation logic surfaces as a
# collection error instead.
_install_stub("proof", ("MiningResponse", "Proof", "FloatArray", "UIntArray"))
_install_stub("zmq")
_install_stub("flatbuffers", ("Builder",))
_install_stub("numpy", ("float32",))

from zmq_pow_writer import (  # noqa: E402
    MiningResponseSubmitter,
    MiningResponseWriter,
    PowEgressConfigError,
    _EGRESS_MODE_BROKER,
    _EGRESS_MODE_LOCAL_MINER,
)


# All POW_* env vars the writer reads. Tests use this list to fully
# scrub the environment before each construction so a stray export
# from the shell can't leak into the config under test.
_POW_ENV_KEYS = (
    "POW_EGRESS_MODE",
    "POW_PROXY_ENABLE",
    "POW_PROXY_PUSH_HOST",
    "POW_PROXY_PUSH_PORT",
    "POW_SAVE_TO_DISK",
    "ZMQ_PUSH_HOST",
    "ZMQ_PUSH_PORT",
)


@pytest.fixture(autouse=True)
def _scrub_pow_env(monkeypatch):
    """Strip every POW/ZMQ env var the writer reads. Each test then
    sets only what it wants to assert on; otherwise a CI env that
    pre-populates ZMQ_PUSH_HOST would cause spurious failures."""
    for key in _POW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestDefaultMode:
    def test_default_egress_mode_is_local_miner(self):
        # Nothing set → local_miner semantics: primary=localhost:7000,
        # proxy disabled, no broker-mode safety net firing.
        w = MiningResponseWriter()
        assert w._egress_mode == _EGRESS_MODE_LOCAL_MINER
        assert w.push_host == "localhost"
        assert w.push_port == 7000
        assert w._proxy_enable is False

    def test_local_miner_preserves_existing_env(self, monkeypatch):
        # The legacy config the k8s minernode wiring uses today.
        monkeypatch.setenv("POW_EGRESS_MODE", "local_miner")
        monkeypatch.setenv("ZMQ_PUSH_HOST", "core-node-0.core-node")
        monkeypatch.setenv("ZMQ_PUSH_PORT", "7000")
        monkeypatch.setenv("POW_PROXY_ENABLE", "true")
        monkeypatch.setenv("POW_PROXY_PUSH_HOST", "miner-proxy")
        monkeypatch.setenv("POW_PROXY_PUSH_PORT", "7002")

        w = MiningResponseWriter()
        assert w._egress_mode == _EGRESS_MODE_LOCAL_MINER
        assert w.push_host == "core-node-0.core-node"
        assert w.push_port == 7000
        assert w._proxy_enable is True
        assert w._proxy_host == "miner-proxy"
        assert w._proxy_port == 7002


class TestBrokerMode:
    def test_broker_mode_default_destination_is_local_proof_collector(self, monkeypatch):
        # Setting POW_EGRESS_MODE=broker with nothing else must select
        # the documented pod-local default (127.0.0.1:7002) — NOT the
        # legacy localhost:7000 Core Node default.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        w = MiningResponseWriter()
        assert w._egress_mode == _EGRESS_MODE_BROKER
        assert w.push_host == "127.0.0.1"
        assert w.push_port == 7002
        assert w._proxy_enable is False

    def test_broker_mode_honours_destination_override(self, monkeypatch):
        # Operator override (e.g. k8s Service DNS for miner-proxy) wins
        # over the 127.0.0.1 default, as long as the override is not a
        # Core Node hostname.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        monkeypatch.setenv("ZMQ_PUSH_HOST", "miner-proxy")
        monkeypatch.setenv("ZMQ_PUSH_PORT", "7002")
        w = MiningResponseWriter()
        assert w.push_host == "miner-proxy"
        assert w.push_port == 7002

    def test_broker_mode_refuses_proxy_enable(self, monkeypatch):
        # Dual-publish would re-introduce the exact leak broker mode
        # exists to prevent: solutions reaching Core Node without the
        # broker closing the lease. Must refuse at construction.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        monkeypatch.setenv("POW_PROXY_ENABLE", "true")
        with pytest.raises(PowEgressConfigError) as exc_info:
            MiningResponseWriter()
        msg = str(exc_info.value)
        assert "POW_PROXY_ENABLE" in msg
        assert "broker" in msg.lower()

    @pytest.mark.parametrize("proxy_value", ["1", "true", "True"])
    def test_broker_mode_refuses_every_truthy_proxy_value(self, monkeypatch, proxy_value):
        # All historically-truthy values must be rejected, not just
        # "true". Mirrors the parser's _TRUTHY_VALUES tuple.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        monkeypatch.setenv("POW_PROXY_ENABLE", proxy_value)
        with pytest.raises(PowEgressConfigError):
            MiningResponseWriter()

    def test_broker_mode_allows_explicit_proxy_disable(self, monkeypatch):
        # "false" is not in _TRUTHY_VALUES — must be accepted.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        monkeypatch.setenv("POW_PROXY_ENABLE", "false")
        w = MiningResponseWriter()
        assert w._egress_mode == _EGRESS_MODE_BROKER
        assert w._proxy_enable is False

    @pytest.mark.parametrize("bad_host", [
        "core-node",
        "core-node-0.core-node",
        "CORE-NODE",  # case-insensitive
        "tensor-core-node.local",
    ])
    def test_broker_mode_refuses_core_node_destination(self, monkeypatch, bad_host):
        # The common misconfiguration the safety net protects against:
        # POW_EGRESS_MODE flipped to broker but ZMQ_PUSH_HOST still
        # pointing at the previous Core Node DNS name.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        monkeypatch.setenv("ZMQ_PUSH_HOST", bad_host)
        with pytest.raises(PowEgressConfigError) as exc_info:
            MiningResponseWriter()
        msg = str(exc_info.value)
        assert "Core Node" in msg or "core-node" in msg.lower()


class TestInvalidMode:
    @pytest.mark.parametrize("bad_mode", [
        "", "broker_mined", "Broker", "LOCAL_MINER",
        "sovereign", "rpc", "1",
    ])
    def test_invalid_egress_mode_raises_value_error(self, monkeypatch, bad_mode):
        monkeypatch.setenv("POW_EGRESS_MODE", bad_mode)
        with pytest.raises(ValueError) as exc_info:
            MiningResponseWriter()
        # Error message names the bad value so operators can grep the log.
        assert "POW_EGRESS_MODE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Slice 3 invariants: regression tests for the three behaviours the
# user's review called out (audit-cache emission preserved in local_miner;
# broker mode never dual-publishes to Core Node; broker mining mode
# suppresses audit emission until the share slice replaces it).
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_start(monkeypatch):
    """Neuter ``MiningResponseWriter.start`` so constructing a
    ``MiningResponseSubmitter`` doesn't spin up the real ZMQ writer
    thread. ``submit_proof_for_audit`` and ``submit_response`` enqueue
    via ``response_queue.put`` regardless of whether the writer is
    running, so the queue itself is the test surface."""
    monkeypatch.setattr(MiningResponseWriter, "start", lambda self: None)


class TestLocalMinerAuditCachePreserved:
    """``submit_proof_for_audit`` MUST still enqueue a ``proxy_only``
    frame when ``local_miner`` is paired with ``POW_PROXY_ENABLE=true``.
    The pull-based audit retrieval (broker /v1/proof → PROOF_REQUEST →
    miner-proxy ProofCache) depends on those audit frames reaching the
    cache. Slice 3 must not have regressed this path while adding the
    broker-mode safety nets.
    """

    def test_local_miner_audit_emits_proxy_only_frame(self, monkeypatch, _no_start):
        monkeypatch.setenv("POW_EGRESS_MODE", "local_miner")
        monkeypatch.setenv("POW_PROXY_ENABLE", "true")
        submitter = MiningResponseSubmitter()
        assert submitter.writer._proxy_enable is True

        ok = submitter.submit_proof_for_audit(req_id=42, proof_dict={"k": "v"})
        assert ok is True
        # Frame queued for the writer thread with proxy_only=True so the
        # _run loop routes it to miner-proxy:7002 rather than Core Node.
        assert submitter.writer.response_queue.qsize() == 1
        frame = submitter.writer.response_queue.get_nowait()
        assert frame["proxy_only"] is True
        assert frame["req_id"] == 42

    def test_local_miner_without_proxy_audit_is_noop(self, monkeypatch, _no_start):
        # local_miner + POW_PROXY_ENABLE unset → no audit channel at all;
        # submit_proof_for_audit must be a no-op (returns True but does
        # not enqueue). Matches the historical behaviour the C++ writer
        # has always had.
        monkeypatch.setenv("POW_EGRESS_MODE", "local_miner")
        submitter = MiningResponseSubmitter()
        assert submitter.writer._proxy_enable is False

        ok = submitter.submit_proof_for_audit(req_id=42, proof_dict={"k": "v"})
        assert ok is True
        assert submitter.writer.response_queue.empty()


class TestBrokerModeNoDualPublish:
    """Broker mode MUST NOT dual-publish anything to Core Node. The
    writer construction surface (``_proxy_enable``, ``_proxy_host``,
    ``_proxy_port``) is the observable invariant — the _run loop
    creates the proxy socket only when ``_proxy_enable`` is true.
    """

    def test_broker_mode_disables_proxy_completely(self, monkeypatch):
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        # Even if env tries to set proxy host/port, broker mode zeroes
        # them out so a future change to _run can't accidentally use them.
        monkeypatch.setenv("POW_PROXY_PUSH_HOST", "core-node-0.core-node")
        monkeypatch.setenv("POW_PROXY_PUSH_PORT", "7000")
        w = MiningResponseWriter()
        assert w._proxy_enable is False
        assert w._proxy_host == ""
        assert w._proxy_port == 0
        # And the primary destination is NOT a Core Node hostname,
        # because broker mode either rejects that at construction or
        # falls back to the 127.0.0.1 default.
        assert "core-node" not in w.push_host.lower()

    def test_broker_mode_primary_default_is_local_proof_collector(self, monkeypatch):
        # The defaulted primary destination MUST be the local
        # ProofCollector — never Core Node, even when env vars are
        # entirely unset.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        w = MiningResponseWriter()
        assert w.push_host == "127.0.0.1"
        assert w.push_port == 7002
        assert "core-node" not in w.push_host.lower()


class TestBrokerModeAuditEnqueue:
    """Broker-mode audit proofs (completion-audit, e.g. the 27B path)
    MUST enqueue a PRIMARY frame stamped ``proof_purpose=audit``.

    What keeps them off the mining path is NOT topology (in broker mode
    the single egress IS the miner-proxy ProofCollector) but the
    explicit ``proof_purpose=audit`` marker in ``model_config_diff`` →
    ``Proof.extra_flags``: the ProofCollector branches on it before its
    mining filters and never feeds MINE_RESULT/MINE_SHARE.

    Earlier this method was a no-op in broker mode, which dropped 27B
    audit proofs entirely. That contract is now intentionally reversed.
    """

    def _drain_audit_frame(self, submitter):
        assert submitter.writer.response_queue.qsize() == 1
        frame = submitter.writer.response_queue.get_nowait()
        # Broker mode uses the primary socket (proxy_only=False) — the
        # collector is the single destination.
        assert frame["proxy_only"] is False
        # proof_purpose=audit must be stamped into model_config_diff so
        # the collector can classify without heuristics.
        mcd = frame["proof_dict"].get("model_config_diff")
        if isinstance(mcd, str):
            mcd = json.loads(mcd)
        assert mcd.get("proof_purpose") == "audit"
        return frame

    def test_broker_mode_audit_enqueues_primary_frame_with_purpose(self, monkeypatch, _no_start):
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        submitter = MiningResponseSubmitter()
        # broker mode forces the dual-publish proxy channel off
        assert submitter.writer._proxy_enable is False

        ok = submitter.submit_proof_for_audit(
            req_id=1001, proof_dict={"completion_id": "c-1"},
        )
        assert ok is True
        frame = self._drain_audit_frame(submitter)
        assert frame["req_id"] == 1001

    def test_broker_mode_audit_merges_into_existing_config_diff(self, monkeypatch, _no_start):
        # When the proof already carries a model_config_diff JSON object,
        # the purpose marker must be merged in, not clobber it.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        submitter = MiningResponseSubmitter()
        ok = submitter.submit_proof_for_audit(
            req_id=7,
            proof_dict={"model_config_diff": {"completion_id": "c-7", "foo": "bar"}},
        )
        assert ok is True
        frame = self._drain_audit_frame(submitter)
        mcd = frame["proof_dict"]["model_config_diff"]
        if isinstance(mcd, str):
            mcd = json.loads(mcd)
        assert mcd["proof_purpose"] == "audit"
        assert mcd["foo"] == "bar"
        assert mcd["completion_id"] == "c-7"

    def test_broker_mode_audit_enqueues_even_with_proxy_env_falsy(self, monkeypatch, _no_start):
        # Explicit POW_PROXY_ENABLE=false in broker mode (what the tf/
        # start-vllm.sh set) must NOT suppress audit emission — the
        # primary socket carries it.
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        monkeypatch.setenv("POW_PROXY_ENABLE", "false")
        submitter = MiningResponseSubmitter()
        ok = submitter.submit_proof_for_audit(req_id=42, proof_dict={})
        assert ok is True
        self._drain_audit_frame(submitter)


class TestBrokerModeShareSubmission:
    """Python submitter parity with the C++ submitter.

    Share-only proofs have a consumer only in broker mode, where the
    writer's primary destination is miner-proxy. local_miner must treat
    submit_share as a no-op so sub-block proofs never reach Core Node.
    """

    def test_broker_mode_submit_share_enqueues_primary_frame(self, monkeypatch, _no_start):
        monkeypatch.setenv("POW_EGRESS_MODE", "broker")
        submitter = MiningResponseSubmitter()
        assert submitter.is_broker_mode() is True

        ok = submitter.submit_share(
            req_id=7,
            nonce=123,
            adjusted_bits=0x1d00ffff,
            pow_blob_hash=b"hash",
            difficulty=42,
            proof_dict={"is_solution": False},
        )
        assert ok is True
        frame = submitter.writer.response_queue.get_nowait()
        assert frame["req_id"] == 7
        assert frame["nonce"] == 123
        assert frame["proxy_only"] is False
        assert frame["proof_dict"]["is_solution"] is False

    def test_local_miner_submit_share_noops(self, monkeypatch, _no_start):
        monkeypatch.setenv("POW_EGRESS_MODE", "local_miner")
        submitter = MiningResponseSubmitter()
        assert submitter.is_broker_mode() is False

        ok = submitter.submit_share(
            req_id=7,
            nonce=123,
            adjusted_bits=0x1d00ffff,
            pow_blob_hash=b"hash",
            difficulty=42,
            proof_dict={"is_solution": False},
        )
        assert ok is True
        assert submitter.writer.response_queue.empty()
