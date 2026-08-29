"""Anthropic Messages API front: parsing/pinning, structure-preserving
compression, verbatim passthrough of responses, streams, and auth."""

import json

import httpx
import respx

from app.gateway.anthropic_routes import (
    assemble_anthropic_messages,
    parse_anthropic_messages,
)
from shared.schemas import SelectionPlan
from tests.conftest import make_unit
from tests.test_gateway_integration import SCORER, _echo_scorer, gateway  # noqa: F401

UPSTREAM = "https://api.anthropic.com"

BIG_LOG = "ERROR retry timeout in charge flow\nINFO css build ok\n" * 40

CLAUDE_CODE_STYLE = [
    {"role": "user", "content": "Fix the retry bug. Constraint: you must not change the public API."},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Let me read the logs."},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cat log.txt"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": BIG_LOG},
    ]},
    {"role": "assistant", "content": "The log shows a retry timeout."},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_0", "content": "old result " * 100},
    ]},
    {"role": "assistant", "content": "Understood."},
    {"role": "user", "content": "Now apply the fix."},
]


# --- parsing / pinning -------------------------------------------------------

def test_trailing_tool_round_and_structural_blocks_pinned():
    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "...", "signature": "sig"},
            {"type": "text", "text": "reading files"},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "fresh result"},
        ]},
    ]
    blocks = parse_anthropic_messages(messages)
    pinned = {b.block_id for b in blocks if b.pinned}
    assert "msg_1" in pinned  # thinking block -> never rewritten
    assert "msg_2" in pinned  # image block -> never rewritten
    assert {"msg_3", "msg_4"} <= pinned  # fresh tool round


def test_flattening_extracts_tool_result_text():
    blocks = parse_anthropic_messages(CLAUDE_CODE_STYLE)
    assert "ERROR retry timeout" in blocks[2].content
    assert blocks[0].pinned  # "must not" keyword
    assert blocks[-1].pinned and blocks[-2].pinned  # current turn


# --- assembly ----------------------------------------------------------------

def _single_unit_plan(blocks, omit_ids):
    units_by_block = {}
    for b in blocks:
        u = make_unit(b, unit_id=f"u_{b.block_id}", text=b.content or " ")
        units_by_block[b.block_id] = [u] if b.content.strip() else []
    plan = SelectionPlan(
        keep=[f"u_{b.block_id}" for b in blocks if b.pinned and b.content.strip()],
        compact=[f"u_{b.block_id}" for b in blocks
                 if not b.pinned and b.content.strip() and b.block_id not in omit_ids],
        omit=[f"u_{bid}" for bid in omit_ids],
    )
    return units_by_block, plan


def test_omitted_tool_result_keeps_structure():
    blocks = parse_anthropic_messages(CLAUDE_CODE_STYLE)
    units_by_block, plan = _single_unit_plan(blocks, omit_ids=["msg_2", "msg_4"])
    out = assemble_anthropic_messages(blocks, units_by_block, plan)

    assert len(out) == len(CLAUDE_CODE_STYLE)
    tool_msg = out[2]
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "toolu_1"
    assert "[CONTEXT OMITTED" in tool_msg["content"][0]["content"]
    # assistant tool_use turn is untouched
    assert out[1] == CLAUDE_CODE_STYLE[1]
    # pinned current turn verbatim
    assert out[-1] == CLAUDE_CODE_STYLE[-1]


def test_compacted_message_gets_unit_text():
    blocks = parse_anthropic_messages(CLAUDE_CODE_STYLE)
    units_by_block, plan = _single_unit_plan(blocks, omit_ids=[])
    # compact everything non-pinned: unit text == flattened content here
    out = assemble_anthropic_messages(blocks, units_by_block, plan)
    assert out[3]["content"] == blocks[3].content  # plain string rewritten to compact text


# --- endpoint ----------------------------------------------------------------

def _anthropic_body(stream=False):
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 400,
        "system": [{"type": "text", "text": "You are Claude Code.",
                    "cache_control": {"type": "ephemeral"}}],
        "messages": CLAUDE_CODE_STYLE,
        "metadata": {"user_id": "cc-session-1"},
        **({"stream": True} if stream else {}),
    }


@respx.mock
def test_messages_compresses_and_forwards_with_auth(gateway):
    client, _ = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    seen = {}

    def upstream(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg_x", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    respx.post(f"{UPSTREAM}/v1/messages").mock(side_effect=upstream)

    resp = client.post("/v1/messages", json=_anthropic_body(), headers={
        "x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025", "authorization": "Bearer oauth-token",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"][0]["text"] == "done"
    assert data["debug"]["api"] == "anthropic"
    assert data["debug"]["session_id"] == "cc-session-1"

    # Auth + protocol headers forwarded verbatim; transport headers dropped.
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["headers"]["authorization"] == "Bearer oauth-token"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025"
    assert seen["headers"]["host"] == "api.anthropic.com"  # httpx sets it, not us

    # System + tools + model forwarded untouched; history compressed in place.
    assert seen["body"]["system"] == _anthropic_body()["system"]
    assert seen["body"]["model"] == "claude-sonnet-4-5"
    assert len(seen["body"]["messages"]) == len(CLAUDE_CODE_STYLE)
    stale = seen["body"]["messages"][4]["content"][0]
    assert stale["type"] == "tool_result" and stale["tool_use_id"] == "toolu_0"


@respx.mock
def test_messages_streaming_passthrough(gateway):
    client, _ = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    sse = (
        b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    respx.post(f"{UPSTREAM}/v1/messages").respond(
        200, content=sse, headers={"content-type": "text/event-stream"}
    )

    resp = client.post("/v1/messages", json=_anthropic_body(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.startswith(": chunkit-debug ")
    # Upstream bytes relayed verbatim after the debug comment.
    assert resp.text.endswith(sse.decode())


@respx.mock
def test_messages_fail_open_forwards_original(gateway, monkeypatch):
    client, _ = gateway
    seen = {}

    def upstream(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant",
                                         "content": [], "stop_reason": "end_turn"})

    respx.post(f"{UPSTREAM}/v1/messages").mock(side_effect=upstream)

    import app.gateway.anthropic_routes as ar

    async def boom(**kwargs):
        raise RuntimeError("scoring exploded")

    monkeypatch.setattr(ar, "rank_and_select", boom)

    resp = client.post("/v1/messages", json=_anthropic_body())
    assert resp.status_code == 200
    assert resp.json()["debug"]["fail_open"] is True
    assert seen["body"]["messages"] == CLAUDE_CODE_STYLE  # untouched


@respx.mock
def test_upstream_error_passes_through(gateway):
    client, _ = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    respx.post(f"{UPSTREAM}/v1/messages").respond(401, json={
        "type": "error", "error": {"type": "authentication_error", "message": "bad key"},
    })
    resp = client.post("/v1/messages", json=_anthropic_body())
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


@respx.mock
def test_count_tokens_and_unknown_paths_pass_through(gateway):
    client, _ = gateway
    respx.post(f"{UPSTREAM}/v1/messages/count_tokens").respond(200, json={"input_tokens": 123})
    respx.get(f"{UPSTREAM}/v1/models").respond(200, json={"data": [{"id": "claude-sonnet-4-5"}]})

    resp = client.post("/v1/messages/count_tokens", json={"model": "m", "messages": []})
    assert resp.json() == {"input_tokens": 123}

    resp = client.get("/v1/models")
    assert resp.json()["data"][0]["id"] == "claude-sonnet-4-5"


@respx.mock
def test_openai_route_still_wins_over_catchall(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"  # handled locally, not proxied


# --- review-round regressions ------------------------------------------------

def test_nested_image_in_tool_result_is_pinned():
    messages = [
        {"role": "user", "content": "start"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t9", "content": [
                {"type": "text", "text": "screenshot follows " * 50},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
            ]},
        ]},
        {"role": "assistant", "content": "noted"},
        {"role": "assistant", "content": "filler " * 100},
        {"role": "user", "content": "continue"},
    ]
    blocks = parse_anthropic_messages(messages)
    assert blocks[1].pinned  # image nested inside tool_result -> never rewritten


def test_rewrite_preserves_cache_control_and_nested_structural():
    from app.gateway.anthropic_routes import _rewrite_content

    content = [
        {"type": "text", "text": "long old text", "cache_control": {"type": "ephemeral"}},
    ]
    out = _rewrite_content(content, "compact")
    assert out[0]["text"] == "compact"
    assert out[0]["cache_control"] == {"type": "ephemeral"}

    nested = [{
        "type": "tool_result", "tool_use_id": "t1",
        "content": [
            {"type": "text", "text": "big log"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        ],
    }]
    out = _rewrite_content(nested, "[marker]")
    assert out[0]["tool_use_id"] == "t1"
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "[marker]"}
    assert parts[1]["type"] == "image"  # never destroyed, even in a rewrite


@respx.mock
def test_query_string_forwarded_on_messages(gateway):
    client, _ = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    seen = {}

    def upstream(request):
        seen["url"] = str(request.url)
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant",
                                         "content": [], "stop_reason": "end_turn"})

    respx.post(f"{UPSTREAM}/v1/messages").mock(side_effect=upstream)

    resp = client.post("/v1/messages?beta=true", json=_anthropic_body())
    assert resp.status_code == 200
    assert "beta=true" in seen["url"]
    assert seen["accept_encoding"] == "identity"  # httpx default gzip suppressed


@respx.mock
def test_stable_marker_bytes_across_turns(gateway):
    client, _ = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    bodies = []

    def upstream(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant",
                                         "content": [], "stop_reason": "end_turn"})

    respx.post(f"{UPSTREAM}/v1/messages").mock(side_effect=upstream)

    for _ in range(2):  # same history resent, as Anthropic clients do each turn
        resp = client.post("/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200

    # Identical forwarded bytes both times: no per-run ids in markers, so the
    # client's prompt-cache prefix can actually hit on the next turn.
    assert bodies[0]["messages"] == bodies[1]["messages"]


@respx.mock
def test_activity_feed_records_both_apis(gateway):
    client, _ = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())
    respx.post(f"{UPSTREAM}/v1/messages").respond(200, json={
        "id": "m", "type": "message", "role": "assistant",
        "content": [], "stop_reason": "end_turn",
    })

    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    client.post("/v1/messages", json=_anthropic_body())

    payload = client.get("/activity").json()
    feed = payload["requests"]
    apis = [r["api"] for r in feed[:2]]
    assert apis == ["anthropic", "openai"]  # newest first
    for r in feed[:2]:
        assert set(r) >= {"ts", "api", "messages", "saved", "savings_percent", "score_source"}
        assert "content" not in r  # sanitised: counters only
        assert isinstance(r["debug"], dict)  # full panel snapshot rides along
        assert "messages" not in r["debug"]  # ...but never message content

    totals = payload["totals"]
    assert totals["requests"] >= 2
    assert totals["claude_requests"] >= 1 and totals["openai_requests"] >= 1
    assert totals["tokens_saved"] >= 0
    assert "dollars_saved" in totals and "history" in totals
    assert len(totals["history"]) >= 2
