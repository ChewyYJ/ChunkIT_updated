"""Real-coding-agent compatibility: OpenAI response shape, streaming,
client tool passthrough, tool-history preservation, shared sessions."""

import json
import sqlite3

import httpx
import respx

import app.gateway.routes as routes
import app.session.store as store
from tests.test_gateway_integration import SCORER, _echo_scorer, _long_messages, gateway  # noqa: F401

TOOLS = [{
    "type": "function",
    "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
}]

TOOL_HISTORY = [
    {"role": "system", "content": "You are a coding agent."},
    {"role": "user", "content": "Read the config file and fix the bug."},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_a", "type": "function",
        "function": {"name": "read_file", "arguments": "{\"path\": \"config.py\"}"},
    }]},
    {"role": "tool", "tool_call_id": "call_a", "content": "TIMEOUT=2.0\nRETRIES=3\n" * 40},
    {"role": "assistant", "content": "The config shows a 2s timeout with 3 retries."},
    {"role": "user", "content": "Now apply the fix."},
]


@respx.mock
def test_response_is_openai_shaped(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 220,
    })
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "stubbed upstream answer"
    assert body["usage"]["prompt_tokens"] == 42
    assert body["debug"]["score_source"] == "nosana"


@respx.mock
def test_client_tools_passed_through_and_retrieval_suppressed(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 120, "tools": TOOLS,
    })
    debug = resp.json()["debug"]
    assert debug["omitted_blocks"] > 0

    last = calls[-1]
    assert last["client_tools"] == TOOLS  # agent's own tools forwarded
    assert last["include_retrieval_tool"] is False  # retrieve_context steps aside

    # Marker text must not tell the model to call a tool it doesn't have.
    for msg in last["messages"]:
        content = msg.get("content") or ""
        if isinstance(content, str) and content.startswith("[CONTEXT OMITTED"):
            assert "retrieve_context" not in content


@respx.mock
def test_tool_history_structure_preserved(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    # Huge budget: nothing omitted. Pinned messages pass through verbatim;
    # non-pinned blocks are forwarded as compacted text but keep structure.
    resp = client.post("/v1/chat/completions", json={
        "messages": TOOL_HISTORY, "target_tokens": 100_000, "tools": TOOLS,
    })
    assert resp.status_code == 200
    sent = calls[-1]["messages"]
    assert len(sent) == len(TOOL_HISTORY)
    assert [m["role"] for m in sent] == [m["role"] for m in TOOL_HISTORY]
    assert sent[0] == TOOL_HISTORY[0]  # pinned system message verbatim
    assert sent[-1] == TOOL_HISTORY[-1]  # pinned current turn verbatim
    assert sent[2]["tool_calls"] == TOOL_HISTORY[2]["tool_calls"]
    assert sent[3]["tool_call_id"] == "call_a"
    # The long tool result is compacted (deduped) but its signal survives.
    assert "TIMEOUT=2.0" in sent[3]["content"]
    assert len(sent[3]["content"]) < len(TOOL_HISTORY[3]["content"])

    # Tight budget: the long tool result is compressed, but its message
    # KEEPS role=tool and tool_call_id so the history stays valid upstream.
    resp = client.post("/v1/chat/completions", json={
        "messages": TOOL_HISTORY, "target_tokens": 60, "tools": TOOLS,
    })
    assert resp.status_code == 200
    sent = calls[-1]["messages"]
    assert len(sent) == len(TOOL_HISTORY)
    assistant_msg, tool_msg = sent[2], sent[3]
    assert assistant_msg["tool_calls"][0]["id"] == "call_a"
    # Structure-only messages pass through verbatim — no fabricated marker.
    assert assistant_msg["content"] is None
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_a"
    assert tool_msg["content"].startswith("[CONTEXT OMITTED")


@respx.mock
def test_shared_session_memory_and_cache_key(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    for ask in ("agent A checking in about the retry bug", "agent B asking about deploy state"):
        resp = client.post("/v1/chat/completions", json={
            "messages": [
                {"role": "system", "content": "shared team session"},
                {"role": "user", "content": ask},
            ],
            "target_tokens": 400,
            "session_id": "team-alpha",
        })
        assert resp.status_code == 200
        assert resp.json()["debug"]["session_id"] == "team-alpha"

    # Both agents used the same cache key -> shared provider prompt cache.
    assert [c["cache_key"] for c in calls[-2:]] == ["team-alpha", "team-alpha"]

    # Both runs' originals coexist in the shared store (run-prefixed block ids).
    conn = sqlite3.connect(store.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT block_id FROM context_blocks_v2 WHERE session_id = ?", ("team-alpha",)
        ).fetchall()
    finally:
        conn.close()
    block_ids = [r[0] for r in rows]
    assert len(block_ids) == 4  # 2 requests x 2 messages, nothing overwritten
    assert len(set(block_ids)) == 4
    assert all(":" in b for b in block_ids)  # run-prefixed to avoid collisions


@respx.mock
def test_streaming_endpoint_returns_sse(gateway, monkeypatch):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    captured = {}

    async def fake_stream(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)

        async def gen():
            yield 'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
            yield "data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(routes, "stream_completion", fake_stream)

    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 220, "stream": True,
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "X-ChunkIT-Run-Id".lower() in {k.lower() for k in resp.headers}
    text = resp.text
    assert text.startswith(": chunkit-debug ")
    debug = json.loads(text.split("\n", 1)[0][len(": chunkit-debug "):])
    assert debug["omitted_blocks"] > 0
    assert 'data: {"choices"' in text
    assert text.rstrip().endswith("data: [DONE]")
    assert captured["include_retrieval_tool"] is True


def test_stream_passes_client_params(gateway, monkeypatch):
    client, calls = gateway
    captured = {}

    async def fake_stream(messages, **kwargs):
        captured.update(kwargs)

        async def gen():
            yield "data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(routes, "stream_completion", fake_stream)

    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "compression_enabled": False,
        "tools": TOOLS,
        "temperature": 0.2,
        "max_tokens": 512,
    })
    assert resp.status_code == 200
    assert captured["client_tools"] == TOOLS
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 512
    assert captured["include_retrieval_tool"] is False


def test_pre_stream_failure_maps_to_http_error(gateway, monkeypatch):
    client, calls = gateway

    async def broken_start(messages, **kwargs):
        raise httpx.ConnectError("upstream down")

    monkeypatch.setattr(routes, "stream_completion", broken_start)

    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True, "compression_enabled": False,
    })
    # Failure before the first SSE byte -> a real HTTP error, not a 200 stream.
    assert resp.status_code == 502
    assert "upstream LLM call failed" in resp.json()["detail"]


def test_mid_stream_failure_delivers_error_in_band(gateway, monkeypatch):
    client, calls = gateway

    async def half_stream(messages, **kwargs):
        async def gen():
            yield 'data: {"choices": [{"delta": {"content": "par"}}]}\n\n'
            raise httpx.ReadError("connection dropped mid-stream")

        return gen()

    monkeypatch.setattr(routes, "stream_completion", half_stream)

    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True, "compression_enabled": False,
    })
    assert resp.status_code == 200  # already committed; error arrives in-band
    assert '"type": "upstream_error"' in resp.text
    assert resp.text.rstrip().endswith("data: [DONE]")


def test_fresh_tool_results_are_pinned():
    from app.gateway.message_parser import parse_messages

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read both configs"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}},
            {"id": "call_2", "type": "function",
             "function": {"name": "read_file", "arguments": "{\"path\": \"b.py\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "contents of a " * 100},
        {"role": "tool", "tool_call_id": "call_2", "content": "contents of b " * 100},
    ]
    blocks = parse_messages(messages)
    pinned = {b.block_id for b in blocks if b.pinned}
    # The whole fresh tool round is pinned: the model has never seen these
    # results, so they must not be compressed on this request.
    assert {"msg_2", "msg_3", "msg_4"} <= pinned
