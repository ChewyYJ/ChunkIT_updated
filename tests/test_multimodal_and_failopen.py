"""Regressions from the compact-tier review: multimodal content must survive
verbatim under any budget, and fail-open must reset every selection artefact."""

import respx

import app.gateway.routes as routes
from app.gateway.message_parser import parse_messages
from tests.test_gateway_integration import SCORER, _echo_scorer, _long_messages, gateway  # noqa: F401

MULTIMODAL_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": [
        {"type": "text", "text": "What is in this screenshot of my failing build?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJDREVGRw=="}},
    ]},
    {"role": "assistant", "content": "It shows a webpack build error in the CSS pipeline. " * 20},
    {"role": "assistant", "content": "Unrelated chatter about the standup schedule. " * 20},
    {"role": "user", "content": "Fix the build error."},
]


def test_multimodal_blocks_are_pinned():
    blocks = parse_messages(MULTIMODAL_MESSAGES)
    by_id = {b.block_id: b for b in blocks}
    assert by_id["msg_1"].pinned  # list-form content can never be rewritten
    assert not by_id["msg_2"].pinned  # plain text stays compressible


@respx.mock
def test_multimodal_content_survives_tight_budget(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    resp = client.post("/v1/chat/completions", json={
        "messages": MULTIMODAL_MESSAGES, "target_tokens": 80,
    })
    assert resp.status_code == 200
    debug = resp.json()["debug"]
    sent = calls[-1]["messages"]

    # The multimodal message reaches the wire structurally intact.
    assert sent[1]["content"] == MULTIMODAL_MESSAGES[1]["content"]
    # And none of its units were "omitted" on paper while shipped in full.
    assert not any(uid for uid in debug["omitted_units"] if ":msg_1" in uid or uid.endswith("msg_1"))
    # Accounting stays honest: estimate tracks the assembled messages.
    assert debug["estimated_tokens"] > 0
    assert not debug["fail_open"]


@respx.mock
def test_fail_open_after_selection_resets_everything(gateway, monkeypatch):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    def boom(*args, **kwargs):
        raise TypeError("assembler exploded after selection")

    monkeypatch.setattr(routes, "assemble_messages", boom)

    messages = _long_messages()
    resp = client.post("/v1/chat/completions", json={
        "messages": messages, "target_tokens": 150,
    })
    assert resp.status_code == 200
    debug = resp.json()["debug"]

    assert debug["fail_open"] is True
    assert debug["sent"] == len(messages)
    assert calls[-1]["messages"] == messages  # clean passthrough
    # No stale selection state, and no retrieval tool for nonexistent markers.
    assert calls[-1]["include_retrieval_tool"] is False
    assert debug["omitted_blocks"] == 0
    assert debug["compacted_blocks"] == 0
    assert debug["kept_units"] == []
    assert debug["compact_units"] == []
    assert debug["omitted_units"] == []
    assert debug["scored_units"] == []
