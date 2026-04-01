"""
Protocol tests for confidential native Responses streaming.

Verifies that when the broker dispatches a confidential job on the native
Responses worker contract (api="responses"), the worker:
  - encrypts every non-function ``response.*`` event into
    ``encrypted_response_event`` frames (only the event taxonomy stays cleartext),
  - bridges REMOTE function calls into the existing ``encrypted_tool_call`` frame,
  - executes WORKER-LOCAL tools in-worker and continues the run INLINE (full
    confidential tool calling, no fall back to chat framing),
  - NEVER emits user content (text, tool arguments, tool output) in the clear.
"""
import json
import sys
import os
from unittest.mock import Mock, AsyncMock

import pytest

# Add src to path for testing (mirrors tests/unit/test_worker_client_unit.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from worker_client import BrokerWorkerClient  # noqa: E402


class _FakeContent:
    """Async-iterable over pre-baked SSE byte lines (mimics aiohttp resp.content)."""

    def __init__(self, lines):
        self._lines = [(l if isinstance(l, bytes) else l.encode("utf-8")) for l in lines]

    def __aiter__(self):
        async def _gen():
            for line in self._lines:
                yield line
        return _gen()


class _FakeResp:
    def __init__(self, lines, status=200):
        self.content = _FakeContent(lines)
        self.headers = {"content-type": "text/event-stream"}
        self.status = status

    async def text(self):
        return ""


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Records POSTs and returns successive pre-baked streams (for inline continuation)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.post_calls = []

    def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return _FakeStreamCtx(self._responses.pop(0))


def _make_client():
    client = BrokerWorkerClient()
    client.ws = AsyncMock()
    client.crypto_service = Mock()
    client.crypto_service.encrypt_response = Mock(return_value="CIPHERTEXT")
    client.tool_configs = {}  # no local worker tools -> everything bridges to client
    return client


def _collect_sent(client):
    return [json.loads(call.args[0]) for call in client.ws.send.call_args_list]


SSE_LINES = [
    'data: {"type":"response.created","response":{"id":"resp_abc"}}',
    'data: {"type":"response.output_item.added","item":{"type":"message","id":"msg_1","role":"assistant"}}',
    'data: {"type":"response.output_text.delta","delta":"Hello"}',
    'data: {"type":"response.output_text.delta","delta":" secret world"}',
    'data: {"type":"response.output_item.done","item":{"type":"message","id":"msg_1","role":"assistant"}}',
    'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"item_1","call_id":"call_1","name":"get_weather"}}',
    'data: {"type":"response.function_call_arguments.delta","item_id":"item_1","delta":"{\\"city\\":"}',
    'data: {"type":"response.function_call_arguments.delta","item_id":"item_1","delta":"\\"Topsecretville\\"}"}',
    'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"item_1","call_id":"call_1","name":"get_weather","arguments":"{\\"city\\":\\"Topsecretville\\"}"}}',
    'data: {"type":"response.completed","response":{"id":"resp_abc","usage":{"input_tokens":10,"output_tokens":5}}}',
    'data: [DONE]',
]


@pytest.mark.asyncio
async def test_confidential_responses_stream_encrypts_and_bridges():
    client = _make_client()
    resp = _FakeResp(SSE_LINES)

    await client._handle_confidential_responses_stream(
        resp,
        job_id="job_1",
        cek=b"0" * 32,
        room_id="room_1",
        confidential_run_id="run_1",
        requested_completion_id="resp_abc",
    )

    frames = _collect_sent(client)
    response_events = [
        f["data"] for f in frames
        if f.get("type") == "CHUNK" and f["data"].get("type") == "encrypted_response_event"
    ]
    tool_calls = [
        f["data"] for f in frames
        if f.get("type") == "CHUNK" and f["data"].get("type") == "encrypted_tool_call"
    ]
    ends = [f for f in frames if f.get("type") == "END"]
    event_types = [e["event_type"] for e in response_events]

    # Text deltas + lifecycle + completed are forwarded; function-call events are not.
    assert event_types.count("response.output_text.delta") == 2
    assert event_types.count("response.completed") == 1
    # FIDELITY: non-function output_item events ARE forwarded...
    assert "response.output_item.added" in event_types
    assert "response.output_item.done" in event_types
    # ...but function-call assembly events are NOT (they bridge to encrypted_tool_call).
    assert "response.function_call_arguments.delta" not in event_types
    for e in response_events:
        assert e["payload_b64"] == "CIPHERTEXT"

    # Exactly one bridged remote tool call, cleartext routing + encrypted body.
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_id"] == "get_weather"
    assert tool_calls[0]["tool_call_id"] == "call_1"
    assert tool_calls[0]["payload_b64"] == "CIPHERTEXT"

    assert len(ends) == 1
    assert ends[0]["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert "response" not in ends[0]

    # SECURITY: no user content in ANY wire frame — only the mocked ciphertext.
    wire = json.dumps(frames)
    assert "Hello" not in wire
    assert "secret world" not in wire
    assert "Topsecretville" not in wire

    # The plaintext WAS handed to the encryptor.
    encrypted_inputs = json.dumps([c.args[0] for c in client.crypto_service.encrypt_response.call_args_list])
    assert "Hello" in encrypted_inputs
    assert "Topsecretville" in encrypted_inputs


@pytest.mark.asyncio
async def test_confidential_responses_requires_encryption_context():
    client = _make_client()
    resp = _FakeResp(['data: [DONE]'])
    with pytest.raises(Exception) as exc:
        await client._handle_responses_api(
            resp, "job_x", {"stream": True, "input": "hi"},
            is_confidential=True, cek=None,
        )
    assert "encryption context" in str(exc.value)


def test_responses_continuation_builder_is_input_shaped():
    """Confidential tool continuation on the Responses path must produce an
    input-shaped follow-up (function_call + function_call_output), not messages."""
    client = _make_client()
    client._remember_confidential_run_payload(
        "run_c", {"model": "m", "input": "what is the weather?", "stream": True}
    )
    client._remember_confidential_tool_call(
        "run_c", "call_1", "get_weather", {"city": "SF"}
    )

    payload = client._build_responses_continuation_payload_from_tool_results(
        "run_c",
        [{"tool_call_id": "call_1", "tool_id": "get_weather", "result": {"success": True, "result": "sunny"}}],
    )

    assert payload is not None
    assert "messages" not in payload
    items = payload["input"]
    assert isinstance(items, list)
    # original input + function_call + function_call_output
    fc = [i for i in items if i.get("type") == "function_call"]
    out = [i for i in items if i.get("type") == "function_call_output"]
    assert len(fc) == 1 and fc[0]["call_id"] == "call_1" and fc[0]["name"] == "get_weather"
    assert len(out) == 1 and out[0]["call_id"] == "call_1" and out[0]["output"] == "sunny"
    assert payload["stream"] is True


@pytest.mark.asyncio
async def test_worker_local_tool_runs_inline_continuation():
    """A worker-local tool executes in-worker and continues the run INLINE via a
    re-POST to the local Responses endpoint — no client tool_call frame emitted."""
    client = _make_client()
    client.tool_configs = {"file_search": {"executor": "file_search"}}
    client._execute_local_worker_tool = AsyncMock(
        return_value={"success": True, "result": "found: report.pdf"}
    )
    client._remember_confidential_run_payload(
        "run_local", {"model": "m", "input": "find my report", "stream": True}
    )

    # Turn 1: model calls the local file_search tool, then completes.
    turn1 = _FakeResp([
        'data: {"type":"response.created","response":{"id":"resp_1"}}',
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"i1","call_id":"call_l","name":"file_search"}}',
        'data: {"type":"response.function_call_arguments.done","item_id":"i1","arguments":"{\\"q\\":\\"report\\"}"}',
        'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"i1","call_id":"call_l","name":"file_search","arguments":"{\\"q\\":\\"report\\"}"}}',
        'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":4,"output_tokens":2}}}',
        'data: [DONE]',
    ])
    # Turn 2 (the inline continuation): model answers using the tool output.
    turn2 = _FakeResp([
        'data: {"type":"response.output_text.delta","delta":"Your report is report.pdf"}',
        'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":9,"output_tokens":6}}}',
        'data: [DONE]',
    ])
    client.http_session = _FakeSession([turn2])

    await client._handle_confidential_responses_stream(
        turn1,
        job_id="job_l",
        cek=b"0" * 32,
        room_id="room_l",
        confidential_run_id="run_local",
        requested_completion_id="resp_1",
        endpoint="http://local/v1/responses",
        request_headers={"Authorization": "Bearer x"},
    )

    # The local tool ran in-worker.
    client._execute_local_worker_tool.assert_awaited_once()
    # Exactly one inline continuation POST happened, carrying an input-shaped body.
    assert len(client.http_session.post_calls) == 1
    cont_body = client.http_session.post_calls[0]["json"]
    assert any(i.get("type") == "function_call_output" for i in cont_body["input"])

    frames = _collect_sent(client)
    # No encrypted_tool_call frame — local tools never go to the client.
    assert not any(
        f.get("type") == "CHUNK" and f["data"].get("type") == "encrypted_tool_call"
        for f in frames
    )
    # The continuation's answer text was streamed (encrypted) and END sent once.
    response_events = [
        f["data"] for f in frames
        if f.get("type") == "CHUNK" and f["data"].get("type") == "encrypted_response_event"
    ]
    assert any(e["event_type"] == "response.output_text.delta" for e in response_events)
    assert len([f for f in frames if f.get("type") == "END"]) == 1

    # SECURITY: neither the tool output nor the answer leak in the clear.
    wire = json.dumps(frames)
    assert "report.pdf" not in wire
