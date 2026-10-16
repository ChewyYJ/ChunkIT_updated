"""Anthropic Messages API front — lets Claude Code point ANTHROPIC_BASE_URL
at ChunkIT.

Architecture (same shape Headroom uses for Claude Code): we stay in Anthropic
format end to end. The request's message history is compressed by the normal
ChunkIT pipeline (Person 2 compressors -> Person 3 scoring -> budget tiers),
then forwarded to the real Anthropic API with the client's own auth headers.
Responses — streaming included — pass back byte-for-byte untouched.

Deliberate scope choices:
- `system` and `tools` are forwarded verbatim (they carry the client's
  prompt-cache breakpoints and tool contracts).
- Messages containing thinking / image / document blocks are pinned: their
  structure must never be rewritten.
- tool_result blocks keep their structure (tool_use_id intact); only their
  inner content is compacted or replaced with a marker.
- No retrieve_context injection on this path: the client owns the tool loop,
  so markers are informational only.
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any
from uuid import uuid4

import httpx
import os

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import key_matches
from app.daytona.client import get_last_prepare_report, prepare_context_blocks
from app.gateway.activity import record as record_activity
from app.gateway.message_parser import has_pinned_keyword
from app.gateway.termcolor import error as log_error
from app.gateway.termcolor import log, warn
from app.gateway.utils import MAX_DEBUG_SCORED_UNITS, estimate_tokens
from app.scoring.service import sanitise_exception
from app.selection.service import rank_and_select
from app.session.store import store_block
from shared.schemas import ContextBlock

router = APIRouter()

UPSTREAM_ENV = "ANTHROPIC_UPSTREAM_URL"
DEFAULT_UPSTREAM = "https://api.anthropic.com"
UPSTREAM_TIMEOUT_SECONDS = 600.0
DEFAULT_ANTHROPIC_TARGET_TOKENS = int(os.getenv("CHUNKIT_ANTHROPIC_TARGET_TOKENS", "6000"))

# Hop-by-hop / transport headers that must not be forwarded upstream.
_DROP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "accept-encoding", "proxy-authorization", "proxy-connection", "te", "upgrade",
    "cookie", "x-chunkit-api-key",
}

# Messages carrying these blocks are pinned outright: rewriting them risks
# structure (thinking signatures, images) or duplicates structured data into
# text (tool_use). The compressible bulk lives in tool RESULTS and plain text.
_STRUCTURAL_BLOCKS = {"thinking", "redacted_thinking", "image", "document", "tool_use"}


def _upstream_base() -> str:
    return os.getenv(UPSTREAM_ENV, DEFAULT_UPSTREAM).rstrip("/")


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _DROP_HEADERS
    }
    authorization = request.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() == "bearer" and key_matches(credential):
        # Strip gateway credentials even when another header authenticated first.
        headers.pop("authorization", None)
    if key_matches(request.headers.get("x-api-key", "")):
        headers.pop("x-api-key", None)
    # Force identity: httpx would otherwise merge its default
    # "accept-encoding: gzip, deflate" into the upstream request, and the
    # streaming relay forwards raw bytes it must be able to prepend to.
    headers["accept-encoding"] = "identity"
    return headers


def _flatten_content(content: Any) -> str:
    """Flatten Anthropic message content into scoring/compression text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "tool_result":
                parts.append(_flatten_content(block.get("content")))
            elif btype == "tool_use":
                try:
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)
                except (TypeError, ValueError):
                    args = "{}"
                parts.append(f"[tool_use {block.get('name', '?')}] {args}")
            # thinking/image/document blocks carry no compressible text
        return "\n".join(p for p in parts if p)
    return str(content)


def _has_block_type(content: Any, types: set[str]) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") in types for b in content)


def _is_structural(content: Any) -> bool:
    """Content whose structure must never be rewritten (thinking, images) —
    including structural blocks NESTED inside tool_result content (e.g. a
    screenshot returned by a Read/browser tool)."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _STRUCTURAL_BLOCKS:
            return True
        if block.get("type") == "tool_result" and _has_block_type(
            block.get("content"), _STRUCTURAL_BLOCKS
        ):
            return True
    return False


def _current_tool_round(messages: list[dict]) -> set[int]:
    """Trailing tool_result user messages plus the assistant tool_use turn
    that produced them — fresh results the model has not seen yet."""
    indexes: set[int] = set()
    j = len(messages) - 1
    while j >= 0 and messages[j].get("role") == "user" and _has_block_type(
        messages[j].get("content"), {"tool_result"}
    ):
        indexes.add(j)
        j -= 1
    if indexes and j >= 0 and messages[j].get("role") == "assistant" and _has_block_type(
        messages[j].get("content"), {"tool_use"}
    ):
        indexes.add(j)
    return indexes


def parse_anthropic_messages(messages: list[dict], block_prefix: str = "") -> list[ContextBlock]:
    n = len(messages)
    tool_round = _current_tool_round(messages)
    blocks: list[ContextBlock] = []
    for i, message in enumerate(messages):
        content = message.get("content")
        text = _flatten_content(content)
        pinned = (
            i == n - 1
            or i == n - 2
            or i in tool_round
            or _is_structural(content)
            or has_pinned_keyword(text)
        )
        blocks.append(
            ContextBlock(
                block_id=f"{block_prefix}msg_{i}",
                role=message.get("role", "user"),
                content=text,
                message_index=i,
                type_hint=None,
                pinned=pinned,
                original_message=dict(message),
            )
        )
    return blocks


def _marker(block: ContextBlock, tokens: int, content_type: str) -> str:
    return f"[CONTEXT OMITTED — {block.block_id}, {content_type}, ~{tokens} tokens.]"


def _rewrite_content(content: Any, new_text: str) -> Any:
    """Replace the compressible text of a message while preserving structure.

    - plain string content -> the new text
    - tool_result blocks keep tool_use_id; their inner content becomes the
      new text (first tool_result carries it; later ones reference it)
    - tool_use blocks are preserved verbatim
    - plain text blocks collapse into the first rewritten slot
    """
    if content is None or isinstance(content, str):
        return new_text
    if not isinstance(content, list):
        return new_text

    rewritten: list[dict] = []
    text_placed = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            rewritten.append(block)
        elif btype == "tool_result":
            new_block = dict(block)
            # Defense in depth: structural blocks nested in a tool_result are
            # pinned upstream, but never destroy one even if a plan slips.
            nested_structural = [
                b for b in (block.get("content") or [])
                if isinstance(b, dict) and b.get("type") in _STRUCTURAL_BLOCKS
            ] if isinstance(block.get("content"), list) else []
            replacement = new_text if not text_placed else "[compacted above]"
            if not text_placed:
                text_placed = True
            if nested_structural:
                new_block["content"] = [{"type": "text", "text": replacement}, *nested_structural]
            else:
                new_block["content"] = replacement
            rewritten.append(new_block)
        elif btype == "text":
            if not text_placed:
                # Copy the block so client metadata (cache_control) survives.
                new_block = dict(block)
                new_block["text"] = new_text
                rewritten.append(new_block)
                text_placed = True
        else:  # structural blocks never reach here (pinned), but keep them safe
            rewritten.append(block)
    if not rewritten:
        return new_text
    if not text_placed:
        rewritten.append({"type": "text", "text": new_text})
    return rewritten


def assemble_anthropic_messages(blocks, units_by_block, plan) -> list[dict]:
    omit_set = set(plan.omit)
    out: list[dict] = []
    for block in sorted(blocks, key=lambda b: b.message_index):
        units = units_by_block.get(block.block_id, [])
        message = dict(block.original_message)
        if block.pinned or not units:
            out.append(message)
            continue
        if all(u.unit_id in omit_set for u in units):
            tokens = sum(u.tokens for u in units)
            content_type = units[0].content_type if units else "text"
            message["content"] = _rewrite_content(
                message.get("content"), _marker(block, tokens, content_type)
            )
        else:
            kept = sorted(
                (u for u in units if u.unit_id not in omit_set),
                key=lambda u: (u.source_start, u.unit_id),
            )
            parts = [u.text for u in kept]
            omitted_tokens = sum(u.tokens for u in units if u.unit_id in omit_set)
            if omitted_tokens:
                parts.append(f"[… ~{omitted_tokens} tokens omitted.]")
            message["content"] = _rewrite_content(message.get("content"), "\n".join(parts))
        out.append(message)
    return out


async def _compress_request_body(body: dict, request: Request, run_id: str) -> tuple[dict, dict]:
    """Compress body['messages']; returns (forwarded_body, debug). Fails open."""
    messages = body.get("messages") or []
    session_id = str((body.get("metadata") or {}).get("user_id") or run_id)
    # Anthropic clients resend the whole history each turn, so msg_N names the
    # SAME message across requests — stable ids (no per-run prefix) keep
    # markers byte-identical between turns, which matters for the client's
    # prompt cache, and make store writes idempotent rather than colliding.
    block_prefix = ""
    original_tokens = sum(estimate_tokens(_flatten_content(m.get("content"))) for m in messages)

    debug: dict = {
        "run_id": run_id,
        "session_id": session_id,
        "api": "anthropic",
        "original_tokens": original_tokens,
        "fail_open": False,
    }

    settings = getattr(request.app.state, "nosana_settings", None)
    scorer_client = getattr(request.app.state, "nosana_client", None)
    started = time.perf_counter()
    try:
        if settings is None or scorer_client is None:
            raise RuntimeError("nosana settings/client not initialised on app state")
        blocks = parse_anthropic_messages(messages, block_prefix)

        daytona_start = time.perf_counter()
        units = await prepare_context_blocks(blocks)
        daytona_ms = round((time.perf_counter() - daytona_start) * 1000, 2)
        prepare_by_block = {e["block_id"]: e for e in get_last_prepare_report()}
        units_by_block: dict[str, list] = {b.block_id: [] for b in blocks}
        for unit in units:
            if unit.text.strip() and unit.block_id in units_by_block:
                units_by_block[unit.block_id].append(unit)

        for b in blocks:
            block_units = units_by_block.get(b.block_id, [])
            metrics = prepare_by_block.get(b.block_id)
            tokens = int(metrics["original_tokens"]) if metrics else sum(u.tokens for u in block_units)
            store_block(session_id, b, tokens, metrics=metrics, units=block_units)

        query = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                query = _flatten_content(message.get("content"))
                break

        outcome = await rank_and_select(
            query=query or "(no objective)",
            blocks=blocks,
            units=[u for us in units_by_block.values() for u in us],
            target_tokens=DEFAULT_ANTHROPIC_TARGET_TOKENS,
            nosana_client=scorer_client,
            settings=settings,
        )
        assembled = assemble_anthropic_messages(blocks, units_by_block, outcome.plan)
        forwarded_tokens = sum(estimate_tokens(_flatten_content(m.get("content"))) for m in assembled)

        omit_set = set(outcome.plan.omit)
        omitted_blocks = sum(
            1 for b in blocks
            if not b.pinned and units_by_block.get(b.block_id)
            and all(u.unit_id in omit_set for u in units_by_block[b.block_id])
        )
        compacted_blocks = sum(
            1 for b in blocks
            if not b.pinned and units_by_block.get(b.block_id)
            and any(u.unit_id not in omit_set for u in units_by_block[b.block_id])
        )
        top_scored = sorted(
            outcome.scored_units, key=lambda s: s.final_priority, reverse=True
        )[:MAX_DEBUG_SCORED_UNITS]
        target = DEFAULT_ANTHROPIC_TARGET_TOKENS

        debug.update({
            "score_source": outcome.score_source,
            "nosana_ms": round(outcome.nosana_ms, 2),
            "daytona_ms": daytona_ms,
            "fallback_reason": outcome.fallback_reason,
            "sent": len(messages),
            "original": len(messages),
            "kept_blocks": len(blocks) - omitted_blocks,
            "compacted_blocks": compacted_blocks,
            "omitted_blocks": omitted_blocks,
            "kept_units": list(outcome.plan.keep),
            "compact_units": list(outcome.plan.compact),
            "omitted_units": list(outcome.plan.omit),
            "forwarded_tokens": forwarded_tokens,
            "tokens_saved": max(0, original_tokens - forwarded_tokens),
            "savings_percent": round(
                max(0, original_tokens - forwarded_tokens) / original_tokens * 100, 1
            ) if original_tokens else 0.0,
            "target_tokens": target,
            "estimated_tokens": outcome.plan.estimated_tokens,
            "over_budget": outcome.plan.estimated_tokens > target,
            "compression_enabled": True,
            "pipeline_ms": round((time.perf_counter() - started) * 1000, 2),
            "scored_units": [
                {"unit_id": s.unit_id, "relevance": round(s.relevance, 4),
                 "final_priority": round(s.final_priority, 4),
                 "pinned": s.unit_id in set(outcome.plan.keep)}
                for s in top_scored
            ],
            "daytona_blocks": get_last_prepare_report(),
        })
        if outcome.fallback_reason:
            warn(run_id, "scoring", f"fallback in use: {outcome.fallback_reason}")
        log(
            run_id, "anthropic",
            f"messages={len(messages)} omitted_units={len(outcome.plan.omit)} "
            f"saved={debug['tokens_saved']} ({debug['savings_percent']}%) "
            f"source={outcome.score_source} session={session_id[:16]}",
        )
        return {**body, "messages": assembled}, debug
    except Exception as e:
        traceback.print_tb(e.__traceback__)
        # Full passthrough: reset every selection artefact so diagnostics
        # stay consistent (mirrors the OpenAI front's fail-open contract).
        debug.update({
            "fail_open": True,
            "fail_reason": sanitise_exception(e),
            "score_source": None,
            "nosana_ms": 0.0,
            "daytona_ms": 0.0,
            "sent": len(messages),
            "original": len(messages),
            "kept_blocks": len(messages),
            "compacted_blocks": 0,
            "omitted_blocks": 0,
            "kept_units": [],
            "compact_units": [],
            "omitted_units": [],
            "forwarded_tokens": original_tokens,
            "tokens_saved": 0,
            "savings_percent": 0.0,
            "target_tokens": DEFAULT_ANTHROPIC_TARGET_TOKENS,
            "estimated_tokens": original_tokens,
            "over_budget": False,
            "scored_units": [],
            "daytona_blocks": [],
            "pipeline_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        log_error(run_id, "anthropic fail-open", debug["fail_reason"])
        return body, debug


async def _forward(request: Request, path: str, content: bytes) -> httpx.Request:
    client: httpx.AsyncClient = request.app.state.http_client
    return client.build_request(
        request.method,
        f"{_upstream_base()}{path}",
        content=content,
        headers=_forward_headers(request),
        params=dict(request.query_params),
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )


@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    run_id = str(uuid4())
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": "request body is not valid JSON"}},
            status_code=400,
        )

    forwarded_body, debug = await _compress_request_body(body, request, run_id)
    record_activity({
        "api": "anthropic",
        "run_id": run_id[:8],
        "session": str(debug.get("session_id", ""))[:24],
        "messages": len(body.get("messages") or []),
        "saved": debug.get("tokens_saved", 0),
        "savings_percent": debug.get("savings_percent", 0.0),
        "original_tokens": debug.get("original_tokens", 0),
        "forwarded_tokens": debug.get("forwarded_tokens", 0),
        "pipeline_ms": debug.get("pipeline_ms", 0.0),
        "score_source": debug.get("score_source"),
        "stream": bool(body.get("stream")),
        "fail_open": debug.get("fail_open", False),
        "debug": debug,
    })

    client: httpx.AsyncClient = request.app.state.http_client
    upstream_request = client.build_request(
        "POST",
        f"{_upstream_base()}/v1/messages",
        json=forwarded_body,
        headers=_forward_headers(request),
        # e.g. ?beta=true from the Anthropic SDK's beta namespace.
        params=request.query_params.multi_items(),
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )

    if body.get("stream"):
        upstream = await client.send(upstream_request, stream=True)
        if upstream.status_code != 200:
            content = await upstream.aread()
            await upstream.aclose()
            return Response(
                content, status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )

        async def relay():
            # SSE comment: spec-compliant, invisible to the Anthropic SDK.
            yield f": chunkit-debug {json.dumps(debug)}\n\n".encode()
            try:
                # aiter_bytes decodes any content-encoding the upstream used,
                # so the client always receives plain SSE bytes.
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            relay(), media_type="text/event-stream",
            headers={"X-ChunkIT-Run-Id": run_id, "Cache-Control": "no-cache"},
        )

    upstream_start = time.perf_counter()
    upstream = await client.send(upstream_request)
    debug["kimi_ms"] = round((time.perf_counter() - upstream_start) * 1000, 2)
    media_type = upstream.headers.get("content-type", "application/json")
    if "application/json" in media_type:
        try:
            data = upstream.json()
            if isinstance(data, dict) and upstream.status_code == 200:
                data["debug"] = debug
            return JSONResponse(data, status_code=upstream.status_code)
        except json.JSONDecodeError:
            pass
    return Response(upstream.content, status_code=upstream.status_code, media_type=media_type)


@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def anthropic_passthrough(path: str, request: Request):
    """Everything else Claude Code calls on its base URL (count_tokens,
    models, ...) forwards verbatim — no compression, no rewriting."""
    upstream_request = await _forward(request, f"/v1/{path}", await request.body())
    client: httpx.AsyncClient = request.app.state.http_client
    upstream = await client.send(upstream_request)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )
