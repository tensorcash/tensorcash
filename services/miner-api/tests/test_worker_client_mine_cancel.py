"""Worker-side tests for the 2026-07-12 mining control-plane fix.

Covers:
- ``BrokerWorkerClient._handle_mine_cancel``: hard-kill on
  ``reason=superseded`` (tombstone + mapping pops + targeted dummy
  cancel), SOFT no-op on ``reason=expired`` (broker keeps a share-grace
  and salvage window open for that lease), unknown-job tolerance.
- req_id tombstone drops in ``_forward_solution`` / ``_forward_share`` /
  ``_on_solution_received``.
- ``_stale_parent_of``: parent comparison in the header-byte (LE-hex)
  domain, fail-open before the context is anchored by a real job.
- ``PriorityRequestManager.cancel_dummy_requests_for_req_id``: cancels
  ONLY the dummy tasks tagged with the cancelled req_id, and gates
  generation for it.

No live ZMQ/WS/vLLM; no ``proof`` FlatBuffer dependency — mappings are
seeded via ``MineRequest.from_dict`` so these run on a bare checkout.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure src + local_mocks are on path BEFORE importing components.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

if "utils.uint256_arithmetics" not in sys.modules:
    utils_pkg = types.ModuleType("utils")
    uint256_mod = types.ModuleType("utils.uint256_arithmetics")
    uint256_mod.set_compact = lambda x: x
    uint256_mod.get_compact = lambda x: x
    uint256_mod.adjust_nbits_by_multiplier = lambda *a, **k: {
        "target_bytes": b"\xff" * 32, "nbits": 0x1d00ffff,
    }
    sys.modules["utils"] = utils_pkg
    sys.modules["utils.uint256_arithmetics"] = uint256_mod

from components import constants  # noqa: E402
from components.context import LockFreeContext  # noqa: E402
from components.mining_protocol import (  # noqa: E402
    MineRequest,
    MINING_MODE_DUMMY_ONLY,
    SUBMIT_POLICY_CORE_NODE,
)
from worker_client import BrokerWorkerClient  # noqa: E402


PREFIX_A = "cd" * 76      # parent LE-hex = "cd"*32 (bytes 4..36)
PREFIX_B = "ab" * 2 + "ee" * 32 + "ab" * 42  # parent LE-hex = "ee"*32
PARENT_A = bytes.fromhex(PREFIX_A)[4:36].hex()
PARENT_B = bytes.fromhex(PREFIX_B)[4:36].hex()


def _wire_request(req_id=42, job_id="job_test", header_prefix=PREFIX_A) -> dict:
    return {
        "type": "MINE_REQUEST",
        "job_id": job_id,
        "work_unit_id": req_id,
        "wallet_id": "wallet_test",
        "network": "test",
        "mode": MINING_MODE_DUMMY_ONLY,
        "model": {"name": "Qwen/Qwen3-8B", "commit": "0" * 40},
        "template": {
            "template_id": f"tmpl_{req_id}",
            "request_id": req_id,
            "block_hash": "aa" * 32,
            "header_prefix": header_prefix,
            "target": "ff" * 32,
            "bits": 0x207fffff,
            "expires_at": 1_700_000_000,
        },
        "policy": {
            "submit_policy": SUBMIT_POLICY_CORE_NODE,
            "max_parallel": 1,
            "user_inference_blocks_on_mining": False,
        },
    }


def _make_client(*, request_manager=None):
    ctx = LockFreeContext("0" * 64, "ffff" * 16)
    with patch.object(constants, "MINING_ENABLED", True), \
         patch.object(constants, "WORKER_TOOLS", []), \
         patch.object(constants, "WORKER_SUPPORTED_MODES", ["plaintext"]):
        client = BrokerWorkerClient(
            context=ctx, zmq_listener=MagicMock(), proof_collector=None,
            request_manager=request_manager,
        )
    client.ws = AsyncMock()
    return client, ctx


def _seed_job(client, req_id=42, job_id="job_test", header_prefix=PREFIX_A):
    """Seed the tracking maps exactly as _handle_mine_request would,
    without the BlockHeader FlatBuffer build (needs the vendored
    ``proof`` package that only ships in the worker image)."""
    request = MineRequest.from_dict(
        _wire_request(req_id=req_id, job_id=job_id, header_prefix=header_prefix)
    )
    client.mining_job_mapping[job_id] = req_id
    client.mining_request_mapping[req_id] = job_id
    client._mining_in_flight[req_id] = request
    client._mining_job_seen_at[req_id] = time.time()
    return request


def _cancel_msg(req_id=42, job_id="job_test", reason="superseded") -> dict:
    return {
        "type": "MINE_CANCEL",
        "job_id": job_id,
        "work_unit_id": req_id,
        "network": "test",
        "reason": reason,
        "valid_parent": "ff" * 32,
    }


class TestHandleMineCancel(unittest.IsolatedAsyncioTestCase):
    async def test_superseded_tombstones_pops_and_cancels_dummies(self):
        rm = MagicMock()
        rm.cancel_dummy_requests_for_req_id = AsyncMock(return_value=2)
        client, _ = _make_client(request_manager=rm)
        _seed_job(client, req_id=42)

        await client._handle_mine_cancel(_cancel_msg(req_id=42))

        self.assertIn(42, client._cancelled_req_ids)
        self.assertNotIn("job_test", client.mining_job_mapping)
        self.assertNotIn(42, client.mining_request_mapping)
        self.assertNotIn(42, client._mining_in_flight)
        self.assertNotIn(42, client._mining_job_seen_at)
        rm.cancel_dummy_requests_for_req_id.assert_awaited_once_with(
            42, reason="broker_cancel:superseded",
        )

    async def test_expired_is_soft_and_keeps_lease_routable(self):
        rm = MagicMock()
        rm.cancel_dummy_requests_for_req_id = AsyncMock(return_value=0)
        client, _ = _make_client(request_manager=rm)
        _seed_job(client, req_id=42)

        await client._handle_mine_cancel(_cancel_msg(req_id=42, reason="expired"))

        # Broker keeps share-grace + salvage windows open for expired
        # leases; the worker must let the in-flight work finish.
        self.assertNotIn(42, client._cancelled_req_ids)
        self.assertIn("job_test", client.mining_job_mapping)
        self.assertIn(42, client._mining_in_flight)
        rm.cancel_dummy_requests_for_req_id.assert_not_awaited()

    async def test_stale_cancel_for_recycled_req_id_is_ignored(self):
        # bcore recycles req_ids: a delayed cancel for job_old naming
        # req_id=42 must NOT kill the fresh job now mining under 42.
        rm = MagicMock()
        rm.cancel_dummy_requests_for_req_id = AsyncMock(return_value=1)
        client, _ = _make_client(request_manager=rm)
        _seed_job(client, req_id=42, job_id="job_new")

        await client._handle_mine_cancel(
            _cancel_msg(req_id=42, job_id="job_old", reason="superseded")
        )

        self.assertNotIn(42, client._cancelled_req_ids)
        self.assertIn("job_new", client.mining_job_mapping)
        self.assertIn(42, client._mining_in_flight)
        rm.cancel_dummy_requests_for_req_id.assert_not_awaited()

    async def test_expired_final_hard_kills(self):
        # The broker's salvage window closed without a result: the
        # follow-up expired_final must hard-kill like superseded.
        rm = MagicMock()
        rm.cancel_dummy_requests_for_req_id = AsyncMock(return_value=1)
        client, _ = _make_client(request_manager=rm)
        _seed_job(client, req_id=42)

        await client._handle_mine_cancel(
            _cancel_msg(req_id=42, reason="expired_final")
        )

        self.assertIn(42, client._cancelled_req_ids)
        self.assertNotIn("job_test", client.mining_job_mapping)
        self.assertNotIn(42, client._mining_in_flight)
        rm.cancel_dummy_requests_for_req_id.assert_awaited_once_with(
            42, reason="broker_cancel:expired_final",
        )

    async def test_unknown_job_is_a_quiet_noop(self):
        client, _ = _make_client()
        await client._handle_mine_cancel(_cancel_msg(req_id=999, job_id="job_ghost"))
        self.assertIn(999, client._cancelled_req_ids)  # still tombstoned

    async def test_missing_req_id_resolves_via_job_mapping(self):
        rm = MagicMock()
        rm.cancel_dummy_requests_for_req_id = AsyncMock(return_value=1)
        client, _ = _make_client(request_manager=rm)
        _seed_job(client, req_id=42)
        msg = _cancel_msg(req_id=42)
        del msg["work_unit_id"]

        await client._handle_mine_cancel(msg)
        self.assertIn(42, client._cancelled_req_ids)

    async def test_unresolvable_req_id_is_ignored(self):
        client, _ = _make_client()
        msg = _cancel_msg()
        del msg["work_unit_id"]
        msg["job_id"] = "job_never_seen"
        await client._handle_mine_cancel(msg)  # must not raise
        self.assertEqual(len(client._cancelled_req_ids), 0)

    async def test_no_request_manager_tombstone_only(self):
        client, _ = _make_client(request_manager=None)
        _seed_job(client, req_id=42)
        await client._handle_mine_cancel(_cancel_msg(req_id=42))
        self.assertIn(42, client._cancelled_req_ids)

    async def test_tombstone_is_bounded(self):
        client, _ = _make_client()
        for rid in range(600):
            client._mark_req_id_cancelled(rid)
        self.assertLessEqual(len(client._cancelled_req_ids), 512)
        self.assertNotIn(0, client._cancelled_req_ids)   # oldest evicted
        self.assertIn(599, client._cancelled_req_ids)


class TestTombstoneDrops(unittest.IsolatedAsyncioTestCase):
    async def test_forward_solution_drops_cancelled_req(self):
        client, _ = _make_client()
        _seed_job(client, req_id=42)
        client._mark_req_id_cancelled(42)

        await client._forward_solution(
            job_id="job_test", req_id=42, result_b64="AAAA",
        )
        client.ws.send.assert_not_awaited()
        self.assertNotIn("job_test", client.mining_job_mapping)
        self.assertNotIn(42, client.mining_request_mapping)

    async def test_forward_share_drops_cancelled_req(self):
        client, _ = _make_client()
        _seed_job(client, req_id=42)
        client._mark_req_id_cancelled(42)

        await client._forward_share(
            job_id="job_test", req_id=42, mining_buf=b"buf", share_b64="AAAA",
        )
        client.ws.send.assert_not_awaited()

    async def test_on_solution_received_drops_cancelled_req(self):
        client, _ = _make_client()
        _seed_job(client, req_id=42)
        client._loop = asyncio.get_running_loop()
        client.running = True
        client._mark_req_id_cancelled(42)

        client._on_solution_received(42, b"buf")
        await asyncio.sleep(0)  # would have scheduled a forward coroutine
        client.ws.send.assert_not_awaited()


class TestStaleParentGuard(unittest.IsolatedAsyncioTestCase):
    def _anchor(self, ctx, *, parent_hex: str, request_id: int):
        ctx.update_mining(parent_hex, PREFIX_A, "ffff" * 16, request_id)

    async def test_fails_open_when_context_unanchored(self):
        client, _ = _make_client()
        request = _seed_job(client, req_id=42, header_prefix=PREFIX_A)
        # Default context: request_id=0 — never anchored by a real job.
        self.assertIsNone(client._stale_parent_of(request))

    async def test_same_parent_passes(self):
        client, ctx = _make_client()
        request = _seed_job(client, req_id=42, header_prefix=PREFIX_A)
        self._anchor(ctx, parent_hex=PARENT_A, request_id=42)
        self.assertIsNone(client._stale_parent_of(request))

    async def test_same_parent_new_req_id_still_passes(self):
        # Same-parent template refresh: old req_id proof stays valid.
        client, ctx = _make_client()
        request = _seed_job(client, req_id=42, header_prefix=PREFIX_A)
        self._anchor(ctx, parent_hex=PARENT_A, request_id=57)
        self.assertIsNone(client._stale_parent_of(request))

    async def test_parent_change_is_detected_in_le_domain(self):
        client, ctx = _make_client()
        request = _seed_job(client, req_id=42, header_prefix=PREFIX_A)
        self._anchor(ctx, parent_hex=PARENT_B, request_id=57)
        stale = client._stale_parent_of(request)
        self.assertIsNotNone(stale)
        self.assertEqual(stale, (PARENT_A, PARENT_B))

    async def test_forward_share_drops_on_stale_parent(self):
        client, ctx = _make_client()
        _seed_job(client, req_id=42, header_prefix=PREFIX_A)
        self._anchor(ctx, parent_hex=PARENT_B, request_id=57)

        await client._forward_share(
            job_id="job_test", req_id=42, mining_buf=b"buf", share_b64="AAAA",
        )
        client.ws.send.assert_not_awaited()

    async def test_forward_solution_drops_on_stale_parent(self):
        client, ctx = _make_client()
        _seed_job(client, req_id=42, header_prefix=PREFIX_A)
        self._anchor(ctx, parent_hex=PARENT_B, request_id=57)

        await client._forward_solution(
            job_id="job_test", req_id=42, result_b64="AAAA",
        )
        client.ws.send.assert_not_awaited()
        self.assertNotIn(42, client._mining_in_flight)


class TestProxyTargetedCancel(unittest.IsolatedAsyncioTestCase):
    """Drives only the new PriorityRequestManager surface; built via
    __new__ so the test doesn't drag the full proxy init (session,
    ModelClient, priority manager) into a unit test."""

    def _manager(self, ctx=None):
        from components.proxy_with_priority import PriorityRequestManager
        mgr = PriorityRequestManager.__new__(PriorityRequestManager)
        mgr.context = ctx or LockFreeContext("0" * 64, "ffff" * 16)
        mgr._dummy_tasks = {}
        mgr._broker_cancelled_req_ids = {}
        return mgr

    def test_req_id_parsing(self):
        from components.proxy_with_priority import PriorityRequestManager as M
        self.assertEqual(M._req_id_of_dummy("resp_dummy_aabbccdd_42_deadbeef"), 42)
        self.assertIsNone(M._req_id_of_dummy("resp_dummy_aabbccdd_42"))
        self.assertIsNone(M._req_id_of_dummy("resp_dummy_aabbccdd_notanum_ff"))
        self.assertIsNone(M._req_id_of_dummy("external_req_123"))

    async def test_cancels_only_matching_req_id(self):
        mgr = self._manager()

        async def _sleeper():
            await asyncio.sleep(30)

        t42a = asyncio.create_task(_sleeper())
        t42b = asyncio.create_task(_sleeper())
        t43 = asyncio.create_task(_sleeper())
        mgr._dummy_tasks = {
            "resp_dummy_aabbccdd_42_00000001": t42a,
            "resp_dummy_aabbccdd_42_00000002": t42b,
            "resp_dummy_aabbccdd_43_00000003": t43,
        }
        try:
            cancelled = await mgr.cancel_dummy_requests_for_req_id(
                42, reason="broker_cancel:superseded",
            )
            self.assertEqual(cancelled, 2)
            self.assertTrue(t42a.cancelled())
            self.assertTrue(t42b.cancelled())
            self.assertFalse(t43.cancelled())
            self.assertIn(42, mgr._broker_cancelled_req_ids)
        finally:
            t43.cancel()
            await asyncio.gather(t43, return_exceptions=True)

    async def test_no_matching_tasks_still_gates_generation(self):
        mgr = self._manager()
        cancelled = await mgr.cancel_dummy_requests_for_req_id(
            42, reason="broker_cancel:superseded",
        )
        self.assertEqual(cancelled, 0)
        self.assertIn(42, mgr._broker_cancelled_req_ids)

    async def test_generation_gate_follows_context_req_id(self):
        ctx = LockFreeContext("0" * 64, "ffff" * 16)
        mgr = self._manager(ctx)
        ctx.update_mining(PARENT_A, PREFIX_A, "ffff" * 16, 42)

        await mgr.cancel_dummy_requests_for_req_id(42, reason="test")
        self.assertTrue(mgr._current_req_id_cancelled())

        # Next MINE_REQUEST re-anchors the context on a live req_id:
        # the gate must lift without any explicit clear.
        ctx.update_mining(PARENT_B, PREFIX_B, "ffff" * 16, 57)
        self.assertFalse(mgr._current_req_id_cancelled())

    async def test_cancelled_set_is_bounded(self):
        mgr = self._manager()
        for rid in range(200):
            await mgr.cancel_dummy_requests_for_req_id(rid, reason="test")
        self.assertLessEqual(len(mgr._broker_cancelled_req_ids), 64)
        self.assertIn(199, mgr._broker_cancelled_req_ids)
        self.assertNotIn(0, mgr._broker_cancelled_req_ids)


if __name__ == "__main__":
    unittest.main()
