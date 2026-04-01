"""Phase 3 worker-side tests for the typed broker-mining protocol.

Covers:
- ``mining_protocol.MineRequest.from_dict``: schema validation, rejection
  paths, and the work_unit_id ↔ template.request_id cross-field invariant.
- ``BrokerWorkerClient._handle_mine_request``: rejects malformed payloads
  with structured errors, builds a BlockHeader FlatBuffer that bcore
  would accept, populates the in-flight cache.
- ``_send_mine_result_typed`` / ``_send_mine_result_raw``: emit the right
  wire shape; ``_forward_solution`` carries the full correlation set.

No live ZMQ or websocket — both are mocked, the proof FlatBuffer schema
is real (it ships with miner-api).
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
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
    MineResult,
    MiningProtocolError,
    MINING_MODE_DUMMY_ONLY,
    SUBMIT_POLICY_CORE_NODE,
)
from worker_client import BrokerWorkerClient  # noqa: E402


# ---------------------------------------------------------------------------
# Shared payload builder
# ---------------------------------------------------------------------------

def _wire_request(req_id=42, **overrides) -> dict:
    base = {
        "type": "MINE_REQUEST",
        "job_id": overrides.get("job_id", "job_test"),
        "work_unit_id": req_id,
        "wallet_id": overrides.get("wallet_id", "wallet_test"),
        "network": overrides.get("network", "test"),
        "mode": overrides.get("mode", MINING_MODE_DUMMY_ONLY),
        "model": {
            "name": "Qwen/Qwen3-8B",
            "commit": "0" * 40,
        },
        "template": {
            "template_id": "tmpl_test",
            "request_id": req_id,
            "block_hash": "aa" * 32,
            "header_prefix": "cd" * 76,
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
    for key, value in overrides.items():
        if key in base:
            base[key] = value
    return base


def _make_client(*, mining_enabled: bool, with_zmq: bool = True):
    ctx = LockFreeContext("0" * 64, "ffff" * 16)
    zmq = MagicMock() if with_zmq else None
    with patch.object(constants, "MINING_ENABLED", mining_enabled), \
         patch.object(constants, "WORKER_TOOLS", []), \
         patch.object(constants, "WORKER_SUPPORTED_MODES", ["plaintext"]):
        client = BrokerWorkerClient(context=ctx, zmq_listener=zmq, proof_collector=None)
    client.ws = AsyncMock()
    return client, ctx, zmq


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

class TestMineRequestSchema(unittest.TestCase):
    def test_from_dict_round_trip(self):
        wire = _wire_request()
        req = MineRequest.from_dict(wire)
        self.assertEqual(req.job_id, "job_test")
        self.assertEqual(req.work_unit_id, 42)
        self.assertEqual(req.network, "test")
        self.assertEqual(req.mode, MINING_MODE_DUMMY_ONLY)
        self.assertEqual(req.model.name, "Qwen/Qwen3-8B")
        self.assertEqual(req.template.request_id, 42)
        self.assertEqual(req.policy.submit_policy, SUBMIT_POLICY_CORE_NODE)

    def test_unknown_mode_rejected(self):
        wire = _wire_request(mode="not-a-mode")
        with self.assertRaises(MiningProtocolError):
            MineRequest.from_dict(wire)

    def test_header_prefix_wrong_length_rejected(self):
        wire = _wire_request()
        wire["template"]["header_prefix"] = "ab" * 10
        with self.assertRaises(MiningProtocolError):
            MineRequest.from_dict(wire)

    def test_work_unit_id_mismatch_rejected(self):
        wire = _wire_request(req_id=42)
        wire["work_unit_id"] = 99  # disagrees with template.request_id=42
        with self.assertRaises(MiningProtocolError):
            MineRequest.from_dict(wire)

    def test_missing_field_rejected(self):
        wire = _wire_request()
        del wire["wallet_id"]
        with self.assertRaises(MiningProtocolError):
            MineRequest.from_dict(wire)

    def test_unknown_submit_policy_rejected(self):
        wire = _wire_request()
        wire["policy"]["submit_policy"] = "rogue-policy"
        with self.assertRaises(MiningProtocolError):
            MineRequest.from_dict(wire)


class TestMineResult(unittest.TestCase):
    def test_to_dict_drops_nones(self):
        r = MineResult(
            job_id="j", work_unit_id=1, wallet_id="w", network="test",
            template_id="t", request_id=1,
        )
        wire = r.to_dict()
        self.assertEqual(wire["type"], "MINE_RESULT")
        self.assertNotIn("solution_b64", wire)
        self.assertNotIn("error", wire)

    def test_to_dict_keeps_set_fields(self):
        r = MineResult(
            job_id="j", work_unit_id=1, wallet_id="w", network="test",
            template_id="t", request_id=1, nonce=42, solution_b64="AAAA",
        )
        wire = r.to_dict()
        self.assertEqual(wire["nonce"], 42)
        self.assertEqual(wire["solution_b64"], "AAAA")
        self.assertNotIn("error", wire)


# ---------------------------------------------------------------------------
# Worker handler integration
# ---------------------------------------------------------------------------

class TestHandleMineRequest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_worker_emits_minimal_result(self):
        client, _, zmq = _make_client(mining_enabled=False)
        await client._handle_mine_request(_wire_request())

        zmq._process_mining_job.assert_not_called()
        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MINE_RESULT")
        self.assertEqual(sent["job_id"], "job_test")
        self.assertEqual(sent["error"], "mining_disabled")
        # Minimal-shape result lacks the typed correlation set; that's OK
        # — broker can still close the lease by job_id.
        self.assertNotIn("solution_b64", sent)

    async def test_invalid_payload_rejected_with_structured_error(self):
        client, _, zmq = _make_client(mining_enabled=True)
        bad = _wire_request()
        bad["template"]["header_prefix"] = "00" * 5  # too short
        await client._handle_mine_request(bad)

        zmq._process_mining_job.assert_not_called()
        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["error"][:15], "invalid_payload")

    async def test_mismatched_work_unit_id_rejected(self):
        client, _, zmq = _make_client(mining_enabled=True)
        bad = _wire_request(req_id=42)
        bad["work_unit_id"] = 99
        await client._handle_mine_request(bad)
        zmq._process_mining_job.assert_not_called()

    async def test_typed_request_builds_flatbuffer_and_tracks_in_flight(self):
        client, _ctx, zmq = _make_client(mining_enabled=True)
        wire = _wire_request(req_id=42)
        await client._handle_mine_request(wire)

        # zmq_listener got a real BlockHeader FlatBuffer carrying the
        # broker's request_id and template fields.
        zmq._process_mining_job.assert_called_once()
        header_fb = zmq._process_mining_job.call_args.args[0]
        self.assertIsInstance(header_fb, (bytes, bytearray))

        from proof import BlockHeader
        block = BlockHeader.BlockHeader.GetRootAs(header_fb, 0)
        self.assertEqual(block.ReqId(), 42)
        # nBits comes from header_prefix[72:76] — that's what the broker
        # signed off and what the FlatBuffer must carry. The wire's
        # separate `bits` field is informational; the prefix is canonical.
        prefix_bytes = bytes.fromhex("cd" * 76)
        expected_nbits = int.from_bytes(prefix_bytes[72:76], "little")
        self.assertEqual(block.Bits(), expected_nbits)
        # prev/merkle vectors match the LE encoding of the prefix's bytes 4..36.
        prev_block_le = bytes(b for b in [block.PrevBlockHash(i) for i in range(32)])
        self.assertEqual(prev_block_le, prefix_bytes[4:36])

        # Tracking populated.
        self.assertEqual(client.mining_job_mapping["job_test"], 42)
        self.assertEqual(client.mining_request_mapping[42], "job_test")
        self.assertIn(42, client._mining_in_flight)
        self.assertEqual(client._mining_in_flight[42].wallet_id, "wallet_test")

        # No MINE_RESULT yet — solution arrives via proof_collector later.
        client.ws.send.assert_not_awaited()

    async def test_zmq_listener_failure_rolls_back_tracking_and_reports(self):
        client, _, zmq = _make_client(mining_enabled=True)
        zmq._process_mining_job.side_effect = RuntimeError("vdf busy")
        wire = _wire_request(req_id=77)
        await client._handle_mine_request(wire)

        # Tracking rolled back.
        self.assertNotIn("job_test", client.mining_job_mapping)
        self.assertNotIn(77, client.mining_request_mapping)
        self.assertNotIn(77, client._mining_in_flight)

        # Typed MINE_RESULT carrying full correlation + processing_error.
        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MINE_RESULT")
        self.assertEqual(sent["job_id"], "job_test")
        self.assertEqual(sent["work_unit_id"], 77)
        self.assertEqual(sent["network"], "test")
        self.assertEqual(sent["template_id"], "tmpl_test")
        self.assertTrue(sent["error"].startswith("processing_error"))


# ---------------------------------------------------------------------------
# Solution forwarding
# ---------------------------------------------------------------------------

class TestForwardSolution(unittest.IsolatedAsyncioTestCase):
    async def test_solution_carries_full_correlation_set(self):
        client, _, zmq = _make_client(mining_enabled=True)
        await client._handle_mine_request(_wire_request(req_id=42))
        client.ws.send.reset_mock()

        # Simulate proof_collector callback: this lookup → forward path.
        await client._forward_solution(
            job_id="job_test", req_id=42, result_b64="AAAA",
        )
        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MINE_RESULT")
        self.assertEqual(sent["job_id"], "job_test")
        self.assertEqual(sent["work_unit_id"], 42)
        self.assertEqual(sent["wallet_id"], "wallet_test")
        self.assertEqual(sent["network"], "test")
        self.assertEqual(sent["template_id"], "tmpl_test")
        self.assertEqual(sent["request_id"], 42)
        self.assertEqual(sent["solution_b64"], "AAAA")
        self.assertNotIn("error", sent)

        # In-flight cache cleared after successful forward.
        self.assertNotIn(42, client._mining_in_flight)

    async def test_solution_for_evicted_lease_emits_no_in_flight_context(self):
        client, _, _ = _make_client(mining_enabled=True)
        # No MINE_REQUEST was processed so _mining_in_flight is empty.
        await client._forward_solution(
            job_id="ghost_job", req_id=99, result_b64="AAAA",
        )
        client.ws.send.assert_awaited_once()
        sent = json.loads(client.ws.send.await_args.args[0])
        self.assertEqual(sent["error"], "no_in_flight_context")
        self.assertEqual(sent["job_id"], "ghost_job")


if __name__ == "__main__":
    unittest.main()
