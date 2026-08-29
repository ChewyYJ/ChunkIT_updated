"""stream_completion / get_completion round behavior: passthrough of
content and client-tool chunks, invisible internal retrieve_context rounds,
and round-budget exhaustion forcing a text answer."""

import json

import app.kimi.client as kimi_client
from app.kimi.client import get_completion, stream_completion


class FakeFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class FakeToolCallDelta:
    def __init__(self, index=0, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


class FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta or FakeDelta()
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, choices, tag):
        self.choices = choices
        self._tag = tag

    def model_dump_json(self):
        return json.dumps({"tag": self._tag})


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class FakeCompletions:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._results.pop(0)


class FakeClient:
    def __init__(self, results):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(results)


def _content_chunk(text, tag):
    return FakeChunk([FakeChoice(FakeDelta(content=text))], tag)


def _finish_chunk(reason, tag):
    return FakeChunk([FakeChoice(FakeDelta(), finish_reason=reason)], tag)


def _role_chunk(tag):  # role-only opening delta: no content, no tool_calls
    return FakeChunk([FakeChoice(FakeDelta())], tag)


def _keepalive_chunk(tag):  # empty choices, as some providers send
    return FakeChunk([], tag)


def _toolcall_chunk(tag, index=0, id=None, name=None, arguments=None):
    fn = FakeFunction(name=name, arguments=arguments)
    return FakeChunk(
        [FakeChoice(FakeDelta(tool_calls=[FakeToolCallDelta(index, id, fn)]))], tag
    )


def _retrieval_stream(tag_prefix, call_id="call_r", context_ids='["msg_3"]'):
    return FakeStream([
        _role_chunk(f"{tag_prefix}-role"),
        _toolcall_chunk(f"{tag_prefix}-t1", id=call_id, name="retrieve_context",
                        arguments=f'{{"context_ids": {context_ids}}}'),
        _finish_chunk("tool_calls", f"{tag_prefix}-f"),
    ])


async def _collect(agen):
    return [line async for line in agen]


def _install(monkeypatch, results):
    fake = FakeClient(results)
    monkeypatch.setattr(kimi_client, "_build_client", lambda: (fake, "test-model", "openai"))
    return fake


async def test_plain_content_streams_through(monkeypatch):
    fake = _install(monkeypatch, [FakeStream([
        _content_chunk("Hel", "c1"), _content_chunk("lo", "c2"), _finish_chunk("stop", "f1"),
    ])])

    lines = await _collect(await stream_completion(
        [{"role": "user", "content": "hi"}], include_retrieval_tool=False,
    ))
    assert lines == [
        'data: {"tag": "c1"}\n\n', 'data: {"tag": "c2"}\n\n', 'data: {"tag": "f1"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert "tools" not in fake.chat.completions.calls[0]


async def test_client_tool_chunks_forwarded_verbatim(monkeypatch):
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    fake = _install(monkeypatch, [FakeStream([
        _toolcall_chunk("t1", id="call_x", name="read_file", arguments="{}"),
        _finish_chunk("tool_calls", "f1"),
    ])])

    lines = await _collect(await stream_completion(
        [{"role": "user", "content": "hi"}],
        include_retrieval_tool=True,  # ignored: client tools win
        client_tools=tools,
    ))
    # The agent's own tool call reaches the client untouched, no extra rounds.
    assert lines == ['data: {"tag": "t1"}\n\n', 'data: {"tag": "f1"}\n\n', "data: [DONE]\n\n"]
    assert fake.chat.completions.calls[0]["tools"] == tools
    assert len(fake.chat.completions.calls) == 1


async def test_internal_retrieval_round_fully_invisible(monkeypatch):
    retrieved = {}

    def fake_retrieve(context_ids, session_id):
        retrieved["ids"] = context_ids
        retrieved["session"] = session_id
        return "ORIGINAL CONTEXT"

    monkeypatch.setattr(kimi_client, "execute_retrieve", fake_retrieve)

    fake = _install(monkeypatch, [
        FakeStream([  # round 1: role delta + keepalive + split tool call
            _role_chunk("r1-role"),
            _keepalive_chunk("r1-keep"),
            _toolcall_chunk("r1-t1", id="call_r", name="retrieve_context",
                            arguments='{"context_ids": ["ms'),
            _toolcall_chunk("r1-t2", arguments='g_3"]}'),
            _finish_chunk("tool_calls", "r1-f"),
        ]),
        FakeStream([  # round 2: final answer
            _role_chunk("r2-role"), _content_chunk("answer", "r2-c"),
            _finish_chunk("stop", "r2-f"),
        ]),
    ])

    lines = await _collect(await stream_completion(
        [{"role": "user", "content": "hi"}],
        session_id="sess-1", include_retrieval_tool=True,
    ))

    # NOTHING from round 1 leaks — not even its role delta or keepalive.
    assert lines == [
        'data: {"tag": "r2-role"}\n\n', 'data: {"tag": "r2-c"}\n\n',
        'data: {"tag": "r2-f"}\n\n', "data: [DONE]\n\n",
    ]
    assert retrieved == {"ids": ["msg_3"], "session": "sess-1"}

    # Round 2 request must carry a valid tool history.
    round2_messages = fake.chat.completions.calls[1]["messages"]
    assert round2_messages[-2]["role"] == "assistant"
    assert round2_messages[-2]["tool_calls"][0]["id"] == "call_r"
    assert round2_messages[-2]["tool_calls"][0]["function"]["arguments"] == '{"context_ids": ["msg_3"]}'
    assert round2_messages[-1] == {
        "role": "tool", "tool_call_id": "call_r", "content": "ORIGINAL CONTEXT",
    }


async def test_narration_before_tool_call_streams_then_retrieves(monkeypatch):
    monkeypatch.setattr(kimi_client, "execute_retrieve", lambda ids, sid: "GOT IT")

    fake = _install(monkeypatch, [
        FakeStream([  # Kimi-style: narrate first, then call the tool
            _role_chunk("r1-role"),
            _content_chunk("Let me check. ", "r1-c"),
            _toolcall_chunk("r1-t", id="call_r", name="retrieve_context",
                            arguments='{"context_ids": ["msg_2"]}'),
            _finish_chunk("tool_calls", "r1-f"),
        ]),
        FakeStream([_content_chunk("Found it.", "r2-c"), _finish_chunk("stop", "r2-f")]),
    ])

    lines = await _collect(await stream_completion(
        [{"role": "user", "content": "hi"}], include_retrieval_tool=True,
    ))
    # Narration (and its role delta) streams; tool machinery stays hidden.
    assert lines == [
        'data: {"tag": "r1-role"}\n\n', 'data: {"tag": "r1-c"}\n\n',
        'data: {"tag": "r2-c"}\n\n', 'data: {"tag": "r2-f"}\n\n', "data: [DONE]\n\n",
    ]
    # The narration is preserved in the follow-up round's assistant message.
    round2_messages = fake.chat.completions.calls[1]["messages"]
    assert round2_messages[-2]["content"] == "Let me check. "


async def test_stream_exhaustion_forces_text_answer(monkeypatch):
    monkeypatch.setattr(kimi_client, "execute_retrieve", lambda ids, sid: "partial")

    fake = _install(monkeypatch, [
        _retrieval_stream("r1"),
        _retrieval_stream("r2", call_id="call_r2"),
        FakeStream([  # final round: tool no longer advertised -> text answer
            _content_chunk("final answer", "r3-c"), _finish_chunk("stop", "r3-f"),
        ]),
    ])

    lines = await _collect(await stream_completion(
        [{"role": "user", "content": "hi"}], include_retrieval_tool=True,
    ))
    assert lines == [
        'data: {"tag": "r3-c"}\n\n', 'data: {"tag": "r3-f"}\n\n', "data: [DONE]\n\n",
    ]
    calls = fake.chat.completions.calls
    assert len(calls) == 3
    assert "tools" in calls[0] and "tools" in calls[1]
    assert "tools" not in calls[2]  # budget spent -> model must answer in text


# --- non-streaming exhaustion ------------------------------------------------

class FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeRespChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message, tag):
        self.choices = [FakeRespChoice(message)]
        self.usage = None
        self._tag = tag

    def model_dump(self):
        return {"tag": self._tag, "object": "chat.completion"}


def _retrieval_response(tag, call_id):
    return FakeResponse(FakeMessage(None, [
        FakeToolCall(call_id, "retrieve_context", '{"context_ids": ["msg_1"]}'),
    ]), tag)


async def test_get_completion_exhaustion_never_returns_internal_tool_calls(monkeypatch):
    monkeypatch.setattr(kimi_client, "execute_retrieve", lambda ids, sid: "partial")

    fake = _install(monkeypatch, [
        _retrieval_response("r1", "call_1"),
        _retrieval_response("r2", "call_2"),
        FakeResponse(FakeMessage("text answer"), "r3"),
    ])

    result = await get_completion(
        [{"role": "user", "content": "hi"}], include_retrieval_tool=True,
    )
    # A no-tools client must never see internal retrieve_context tool_calls.
    assert result["response"]["tag"] == "r3"
    assert result["retrieval_rounds"] == 2
    calls = fake.chat.completions.calls
    assert len(calls) == 3
    assert "tools" in calls[0] and "tools" in calls[1]
    assert "tools" not in calls[2]
