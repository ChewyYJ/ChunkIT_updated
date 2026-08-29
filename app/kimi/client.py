import json
import os
from collections.abc import AsyncIterator
from typing import Any, Optional

from openai import AsyncOpenAI

from app.gateway.termcolor import log
from app.kimi.tools import RETRIEVE_CONTEXT_TOOL, execute_retrieve

MAX_RETRIEVAL_ROUNDS = int(os.getenv("CHUNKIT_MAX_RETRIEVAL_ROUNDS", "2"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("CHUNKIT_UPSTREAM_TIMEOUT_SECONDS", "30"))


def _build_client() -> tuple[AsyncOpenAI, str, str]:
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        return AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS), model, provider

    if provider == "kimi":
        api_key = os.getenv("MOONSHOT_API_KEY")
        if not api_key:
            raise RuntimeError("MOONSHOT_API_KEY is required when LLM_PROVIDER=kimi")
        base_url = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
        model = os.getenv("KIMI_MODEL", "kimi-k3")
        return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS), model, provider

    raise RuntimeError(f"Unsupported LLM_PROVIDER: {provider}")


def _extract_cached_tokens(usage) -> int:
    if usage is None:
        return 0
    cached = getattr(usage, "cached_tokens", None)
    if cached is not None:
        return cached
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is not None:
            return cached
    return 0


def _completion_kwargs(
    model: str,
    provider: str,
    messages: list[dict],
    *,
    stream: bool,
    tools: Optional[list[dict]],
    tool_choice: Any,
    temperature: Optional[float],
    max_tokens: Optional[int],
    cache_key: Optional[str],
) -> dict:
    kwargs: dict = dict(model=model, messages=messages, stream=stream)
    if tools:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    # Stable across a shared session -> real cache reuse between agents.
    if provider == "kimi" and cache_key:
        kwargs["prompt_cache_key"] = cache_key
    return kwargs


def _resolve_tools(client_tools: Optional[list[dict]], include_retrieval_tool: bool) -> tuple[Optional[list[dict]], bool]:
    """Client tools always win; retrieve_context is only injected when the
    client brought no tools of its own (mixing internal and client tool calls
    in one round is fragile across providers)."""
    if client_tools:
        return client_tools, False
    if include_retrieval_tool:
        return [RETRIEVE_CONTEXT_TOOL], True
    return None, False


def _run_retrieval(tool_call_id: str, name: str, arguments: str,
                   cache_key: Optional[str], session_id: Optional[str],
                   round_number: int) -> dict:
    if name != "retrieve_context":
        return {"role": "tool", "tool_call_id": tool_call_id,
                "content": f"[tool {name} is not available]"}
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    context_ids = args.get("context_ids", [])
    log(cache_key, "retrieval", f"round {round_number}: context_ids={context_ids}")
    return {"role": "tool", "tool_call_id": tool_call_id,
            "content": execute_retrieve(context_ids, session_id or cache_key or "")}


async def get_completion(
    messages: list[dict],
    cache_key: str | None = None,
    session_id: str | None = None,
    include_retrieval_tool: bool = True,
    client_tools: list[dict] | None = None,
    tool_choice: Any = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Non-streaming completion. Returns the upstream OpenAI-shaped response
    body (model_dump) plus retrieval metadata.

    Internal retrieve_context rounds never leak to the client: once the round
    budget is spent, the retrieval tool is no longer advertised, so the final
    upstream call must produce a normal text answer."""
    client, model, provider = _build_client()
    working_messages = list(messages)
    tools, internal_retrieval = _resolve_tools(client_tools, include_retrieval_tool)
    retrieval_rounds = 0

    while True:
        advertise = bool(client_tools) or (
            internal_retrieval and retrieval_rounds < MAX_RETRIEVAL_ROUNDS
        )
        response = await client.chat.completions.create(
            **_completion_kwargs(
                model, provider, working_messages,
                stream=False, tools=tools if advertise else None,
                tool_choice=tool_choice if advertise else None,
                temperature=temperature, max_tokens=max_tokens, cache_key=cache_key,
            )
        )
        message = response.choices[0].message
        tool_calls = (message.tool_calls or []) if internal_retrieval else []

        # Client tool calls belong to the caller — never intercepted.
        if not tool_calls:
            return {
                "response": response.model_dump(),
                "retrieval_rounds": retrieval_rounds,
                "cached_tokens": _extract_cached_tokens(response.usage),
            }

        working_messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            working_messages.append(
                _run_retrieval(tc.id, tc.function.name, tc.function.arguments,
                               cache_key, session_id, retrieval_rounds + 1)
            )
        retrieval_rounds += 1


def _sse(chunk) -> str:
    return f"data: {chunk.model_dump_json()}\n\n"


async def stream_completion(
    messages: list[dict],
    cache_key: str | None = None,
    session_id: str | None = None,
    include_retrieval_tool: bool = True,
    client_tools: list[dict] | None = None,
    tool_choice: Any = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """Streaming completion as OpenAI-style SSE lines.

    The FIRST upstream request happens eagerly in this coroutine, so
    pre-stream failures (bad key, 401/429) raise before any SSE bytes are
    sent and can map to proper HTTP errors.

    Client tool-call chunks pass through verbatim. Internal retrieve_context
    rounds are invisible: a round is buffered until its first meaningful
    delta classifies it — pure tool-call rounds are discarded wholesale
    (role-only deltas and keepalives included) while content-first rounds
    stream live. Once the round budget is spent the retrieval tool is no
    longer advertised, so the final round always streams a normal answer
    with its finish_reason intact."""
    client, model, provider = _build_client()
    tools, internal_retrieval = _resolve_tools(client_tools, include_retrieval_tool)
    working_messages = list(messages)

    def _kwargs(advertise: bool) -> dict:
        return _completion_kwargs(
            model, provider, working_messages,
            stream=True, tools=tools if advertise else None,
            tool_choice=tool_choice if advertise else None,
            temperature=temperature, max_tokens=max_tokens, cache_key=cache_key,
        )

    first_advertise = bool(client_tools) or (internal_retrieval and MAX_RETRIEVAL_ROUNDS > 0)
    first_stream = await client.chat.completions.create(**_kwargs(first_advertise))

    async def generate() -> AsyncIterator[str]:
        stream = first_stream
        rounds = 0
        while True:
            interceptable = internal_retrieval and rounds < MAX_RETRIEVAL_ROUNDS
            pending: dict[int, dict] = {}
            content_parts: list[str] = []
            held: list = []      # chunks buffered until the round is classified
            mode: str | None = None  # None (undecided) | "content" | "internal"

            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta if choice else None

                if not interceptable:
                    yield _sse(chunk)
                    continue

                if delta is not None and delta.tool_calls:
                    # Only retrieve_context can appear here (no client tools).
                    if mode is None:
                        mode = "internal"
                        held.clear()  # discard the round's role/keepalive deltas
                    for tc in delta.tool_calls:
                        slot = pending.setdefault(
                            tc.index or 0, {"id": None, "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
                    continue

                if choice is not None and choice.finish_reason and pending:
                    continue  # the internal round's finish chunk

                if delta is not None and delta.content:
                    content_parts.append(delta.content)
                    if mode is None:
                        mode = "content"
                        for held_chunk in held:
                            yield _sse(held_chunk)
                        held.clear()
                    if mode == "content":
                        yield _sse(chunk)
                    continue

                # Role-only deltas, keepalives, finish of a content round.
                if mode is None:
                    held.append(chunk)
                elif mode == "content":
                    yield _sse(chunk)
                # mode == "internal": suppressed

            if pending and rounds < MAX_RETRIEVAL_ROUNDS:
                ordered = [slot for _, slot in sorted(pending.items())]
                working_messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": [
                        {
                            "id": slot["id"] or f"call_{i}",
                            "type": "function",
                            "function": {"name": slot["name"], "arguments": slot["arguments"]},
                        }
                        for i, slot in enumerate(ordered)
                    ],
                })
                for i, slot in enumerate(ordered):
                    working_messages.append(
                        _run_retrieval(slot["id"] or f"call_{i}", slot["name"],
                                       slot["arguments"], cache_key, session_id, rounds + 1)
                    )
                rounds += 1
                advertise = bool(client_tools) or (
                    internal_retrieval and rounds < MAX_RETRIEVAL_ROUNDS
                )
                stream = await client.chat.completions.create(**_kwargs(advertise))
                continue

            # A round that never got classified (role/keepalive chunks only).
            for held_chunk in held:
                yield _sse(held_chunk)
            break

        yield "data: [DONE]\n\n"

    return generate()
