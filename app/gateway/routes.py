from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from openai import APIConnectionError, APITimeoutError, AuthenticationError, BadRequestError, RateLimitError
from pydantic import BaseModel, Field, field_validator

from app.daytona.client import get_last_prepare_report, prepare_context_blocks
from app.gateway.activity import record as record_activity
from app.gateway.assembler import assemble_messages, count_compacted_blocks, count_omitted_blocks
from app.gateway.message_parser import parse_messages, summarize_split
from app.gateway.termcolor import error as log_error
from app.gateway.termcolor import log, warn
from app.gateway.utils import MAX_DEBUG_SCORED_UNITS, estimate_tokens
from app.kimi.client import get_completion, stream_completion
from app.scoring.service import extract_query, sanitise_exception
from app.selection.service import rank_and_select
from app.session.store import store_block

router = APIRouter()

DEFAULT_TARGET_TOKENS = int(os.getenv("CHUNKIT_TARGET_TOKENS", "2000"))


class ChatCompletionRequest(BaseModel):
    messages: list[dict]
    target_tokens: int | None = None
    compression_enabled: bool = True
    # OpenAI-compatible passthrough fields (agent clients send these).
    stream: bool = False
    tools: list[dict] | None = None
    tool_choice: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    model: str | None = None  # accepted for compatibility; provider model is pinned via env
    # Shared-memory key: agents passing the same session_id share the original-
    # context store and (on Kimi) the prompt cache.
    session_id: str | None = Field(default=None, max_length=128)

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, v: list[dict]) -> list[dict]:
        if not v:
            raise ValueError("messages must not be empty")
        for i, msg in enumerate(v):
            if not isinstance(msg, dict) or "role" not in msg:
                raise ValueError(f"message at index {i} must include 'role'")
            if "content" not in msg and "tool_calls" not in msg:
                raise ValueError(f"message at index {i} must include 'content' or 'tool_calls'")
        return v

    @field_validator("target_tokens")
    @classmethod
    def validate_target_tokens(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("target_tokens must be a positive integer")
        return v


def _estimate_message_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        total += estimate_tokens(content)
    return total


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    run_id = str(uuid4())
    # A client-supplied session_id makes memory shared across agents/requests;
    # block ids are then prefixed per run so stored originals never collide.
    session_id = body.session_id or run_id
    block_prefix = f"{run_id[:8]}:" if body.session_id else ""
    target_tokens = body.target_tokens if body.target_tokens is not None else DEFAULT_TARGET_TOKENS
    original_tokens = _estimate_message_tokens(body.messages)

    settings = getattr(request.app.state, "nosana_settings", None)
    scorer_client = getattr(request.app.state, "nosana_client", None)

    pipeline_start = time.perf_counter()
    fail_open = False
    fail_reason = None
    score_source = None
    nosana_ms = 0.0
    daytona_ms = 0.0
    daytona_report: list[dict] = []
    fallback_reason = None
    scored_units_debug: list[dict] = []
    kept_units: list[str] = []
    compact_units: list[str] = []
    omitted_units: list[str] = []
    compacted_blocks = 0
    retrieval_available = False

    if not body.compression_enabled:
        # Passthrough baseline mode for fair benchmark comparison.
        score_source = "disabled"
        assembled_messages = body.messages
        omitted_blocks = 0
        kept_blocks = len(body.messages)
        estimated_tokens = original_tokens
    else:
        try:
            if settings is None or scorer_client is None:
                raise RuntimeError("nosana settings/client not initialised on app state")

            blocks = parse_messages(body.messages, block_prefix)
            summary = summarize_split(blocks)
            log(run_id, "context-split", summary)

            # Person 2 pipeline: detect type, redact, compress, split each
            # block into evidence-sized ContextUnits (local or Daytona remote).
            daytona_start = time.perf_counter()
            units = await prepare_context_blocks(blocks)
            daytona_ms = round((time.perf_counter() - daytona_start) * 1000, 2)
            daytona_report = get_last_prepare_report()
            prepare_by_block = {entry["block_id"]: entry for entry in daytona_report}

            # Blank units (e.g. tool_calls-only assistant messages) carry no
            # compressible content — dropping them means the block passes
            # through verbatim instead of gaining a fabricated marker.
            units_by_block: dict[str, list] = {b.block_id: [] for b in blocks}
            for unit in units:
                if unit.text.strip() and unit.block_id in units_by_block:
                    units_by_block[unit.block_id].append(unit)

            for b in blocks:
                block_units = units_by_block.get(b.block_id, [])
                metrics = prepare_by_block.get(b.block_id)
                block_tokens = (
                    int(metrics["original_tokens"]) if metrics
                    else sum(u.tokens for u in block_units)
                )
                store_block(session_id, b, block_tokens, metrics=metrics, units=block_units)

            all_units = [u for block_units in units_by_block.values() for u in block_units]
            query = extract_query(body.messages)

            # Person 3 scoring: live Nosana reranker with deterministic fallback.
            outcome = await rank_and_select(
                query=query,
                blocks=blocks,
                units=all_units,
                target_tokens=target_tokens,
                nosana_client=scorer_client,
                settings=settings,
            )
            plan = outcome.plan
            score_source = outcome.score_source
            nosana_ms = round(outcome.nosana_ms, 2)
            fallback_reason = outcome.fallback_reason
            if fallback_reason:
                warn(run_id, "scoring", f"fallback in use: {fallback_reason}")

            omitted_blocks = count_omitted_blocks(blocks, units_by_block, plan)
            compacted_blocks = count_compacted_blocks(blocks, units_by_block, plan)
            # retrieve_context is only advertised when something was omitted
            # (fully, or partially inside a compacted block) AND the client
            # brought no tools of its own — every marker/note the model sees
            # must reference a tool it can actually call.
            retrieval_available = bool(plan.omit) and not body.tools
            assembled_messages = assemble_messages(
                blocks, units_by_block, plan, retrieval_available=retrieval_available
            )

            kept_blocks = len(blocks) - omitted_blocks
            estimated_tokens = plan.estimated_tokens
            kept_units = list(plan.keep)
            compact_units = list(plan.compact)
            omitted_units = list(plan.omit)

            # Sanitised for debug/dashboard: ids and scores only, never text.
            top_scored = sorted(
                outcome.scored_units, key=lambda s: s.final_priority, reverse=True
            )[:MAX_DEBUG_SCORED_UNITS]
            pinned_unit_ids = set(plan.keep)
            scored_units_debug = [
                {
                    "unit_id": s.unit_id,
                    "relevance": round(s.relevance, 4),
                    "final_priority": round(s.final_priority, 4),
                    # Pinned units carry sentinel 1.0 scores, not real ones.
                    "pinned": s.unit_id in pinned_unit_ids,
                }
                for s in top_scored
            ]
        except Exception as e:
            # Stack only — the exception message itself may embed raw context
            # (e.g. pydantic input previews), so it is logged sanitised below.
            traceback.print_tb(e.__traceback__)
            fail_open = True
            fail_reason = sanitise_exception(e)
            log_error(run_id, "fail-open", fail_reason)
            # Full passthrough: reset EVERY selection artefact so the debug
            # output is consistent and retrieve_context is never advertised
            # for markers that do not exist in the forwarded messages.
            assembled_messages = body.messages
            omitted_blocks = 0
            compacted_blocks = 0
            kept_blocks = len(body.messages)
            estimated_tokens = original_tokens
            score_source = None
            nosana_ms = 0.0
            retrieval_available = False
            kept_units = []
            compact_units = []
            omitted_units = []
            scored_units_debug = []

    pipeline_ms = round((time.perf_counter() - pipeline_start) * 1000, 2)

    forwarded_tokens = _estimate_message_tokens(assembled_messages)
    tokens_saved = max(0, original_tokens - forwarded_tokens)
    savings_percent = round(tokens_saved / original_tokens * 100, 1) if original_tokens else 0.0

    debug = {
        "run_id": run_id,
        "session_id": session_id,
        "score_source": score_source,
        "nosana_ms": nosana_ms,
        "daytona_ms": daytona_ms,
        "fallback_reason": fallback_reason,
        "sent": len(assembled_messages),
        "original": len(body.messages),
        "omitted_blocks": omitted_blocks,
        "compacted_blocks": compacted_blocks,
        "kept_blocks": kept_blocks,
        "kept_units": kept_units,
        "compact_units": compact_units,
        "omitted_units": omitted_units,
        "original_tokens": original_tokens,
        "forwarded_tokens": forwarded_tokens,
        "tokens_saved": tokens_saved,
        "savings_percent": savings_percent,
        "estimated_tokens": estimated_tokens,
        "target_tokens": target_tokens,
        "over_budget": bool(
            body.compression_enabled and not fail_open and estimated_tokens > target_tokens
        ),
        "compression_enabled": body.compression_enabled,
        "fail_open": fail_open,
        "pipeline_ms": pipeline_ms,
        "scored_units": scored_units_debug,
        # Person 2 per-block provenance (hashes and counters, no content).
        "daytona_blocks": daytona_report,
    }
    if fail_open:
        debug["fail_reason"] = fail_reason

    log(
        run_id,
        "pipeline",
        f"sent={len(assembled_messages)}/{len(body.messages)} omitted={omitted_blocks} "
        f"compacted={compacted_blocks} est_tokens={estimated_tokens} target={target_tokens} "
        f"source={score_source} daytona={daytona_ms}ms saved={tokens_saved} ({savings_percent}%) "
        f"stream={body.stream} fail_open={fail_open}",
    )

    record_activity({
        "api": "openai",
        "run_id": run_id[:8],
        "session": session_id[:24],
        "messages": len(body.messages),
        "saved": tokens_saved,
        "savings_percent": savings_percent,
        "original_tokens": original_tokens,
        "forwarded_tokens": forwarded_tokens,
        "pipeline_ms": pipeline_ms,
        "score_source": score_source,
        "stream": body.stream,
        "fail_open": fail_open,
        # Sanitised snapshot (ids/counters/provenance, no content) so the
        # dashboard can render the full panel set for live traffic.
        "debug": debug,
    })

    include_retrieval_tool = retrieval_available
    upstream_kwargs = dict(
        cache_key=session_id,
        session_id=session_id,
        include_retrieval_tool=include_retrieval_tool,
        client_tools=body.tools,
        tool_choice=body.tool_choice,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )

    if body.stream:
        # The first upstream request happens inside stream_completion() before
        # any SSE bytes are sent, so pre-stream failures map to real HTTP
        # errors instead of an in-band error under a committed 200.
        try:
            upstream = await stream_completion(assembled_messages, **upstream_kwargs)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except (AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError, BadRequestError) as e:
            raise HTTPException(status_code=502, detail=f"upstream LLM call failed: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"upstream LLM call failed: {e}")

        async def sse():
            # SSE comment line: spec-compliant, ignored by OpenAI SSE parsers.
            yield f": chunkit-debug {json.dumps(debug)}\n\n"
            try:
                async for line in upstream:
                    yield line
            except Exception as e:
                # Mid-stream failure: the 200 is committed, deliver in-band.
                payload = {"error": {"message": f"upstream LLM call failed: {sanitise_exception(e)}",
                                     "type": "upstream_error"}}
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"X-ChunkIT-Run-Id": run_id, "Cache-Control": "no-cache"},
        )

    kimi_start = time.perf_counter()
    try:
        result = await get_completion(assembled_messages, **upstream_kwargs)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except (AuthenticationError, RateLimitError, APIConnectionError, APITimeoutError, BadRequestError) as e:
        raise HTTPException(status_code=502, detail=f"upstream LLM call failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream LLM call failed: {e}")
    kimi_ms = round((time.perf_counter() - kimi_start) * 1000, 2)

    debug["kimi_ms"] = kimi_ms
    debug["retrieval_count"] = result.get("retrieval_rounds", 0)
    debug["cached_tokens"] = result.get("cached_tokens", 0)

    # OpenAI-shaped response body (id/choices/usage/...) plus ChunkIT debug.
    response_body = result["response"]
    response_body["debug"] = debug
    return response_body
