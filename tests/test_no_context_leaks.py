"""Spec 13.1: captured logs and debug output must never contain raw context
text, and degenerate inputs must degrade the score — not the whole request."""

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.nosana.client import MAX_QUERY_CHARS, MAX_UNIT_CHARS, NosanaScorerClient
from app.nosana.schemas import ScoreItem
from app.scoring.service import obtain_relevance_scores, sanitise_exception
from tests.conftest import make_block, make_settings, make_unit

BASE = "http://scorer.test"
SENTINEL = "CUSTOMER-PII-SENTINEL"


def _echo(request):
    payload = json.loads(request.content)
    return httpx.Response(200, json={
        "model": "m", "device": "cpu", "latency_ms": 1.0,
        "scores": [
            {"unit_id": item["unit_id"], "relevance": 0.5, "truncated": False}
            for item in payload["items"]
        ],
    })


async def _score(settings, query, units):
    async with httpx.AsyncClient() as http_client:
        client = NosanaScorerClient(settings, http_client)
        return await obtain_relevance_scores(query, units, client, settings)


@respx.mock
async def test_oversized_unit_is_truncated_not_fallback():
    seen = {}

    def responder(request):
        payload = json.loads(request.content)
        seen["max_len"] = max(len(item["text"]) for item in payload["items"])
        return _echo(request)

    respx.post(f"{BASE}/score").mock(side_effect=responder)

    block = make_block(0, content="x")
    huge = make_unit(block, unit_id="u_big", text=SENTINEL + "A" * 30_000)
    scores, source, ms, reason = await _score(make_settings(), "find the bug", [huge])

    assert source == "nosana", f"oversized unit must stay on the live path: {reason}"
    assert seen["max_len"] <= MAX_UNIT_CHARS
    assert scores == {"u_big": 0.5}


@respx.mock
async def test_blank_unit_scored_zero_without_wire_call_for_it():
    seen = {}

    def responder(request):
        payload = json.loads(request.content)
        seen["ids"] = [item["unit_id"] for item in payload["items"]]
        return _echo(request)

    respx.post(f"{BASE}/score").mock(side_effect=responder)

    b0, b1 = make_block(0, content="   "), make_block(1, content="real content here")
    blank = make_unit(b0, unit_id="u_blank", text="   ")
    normal = make_unit(b1, unit_id="u_normal")
    scores, source, ms, reason = await _score(make_settings(), "query", [blank, normal])

    assert source == "nosana"
    assert seen["ids"] == ["u_normal"]  # blank never leaves the process
    assert scores == {"u_blank": 0.0, "u_normal": 0.5}


@respx.mock
async def test_oversized_query_is_clamped():
    seen = {}

    def responder(request):
        seen["query_len"] = len(json.loads(request.content)["query"])
        return _echo(request)

    respx.post(f"{BASE}/score").mock(side_effect=responder)

    unit = make_unit(make_block(0, content="content"), unit_id="u0")
    _, source, _, _ = await _score(make_settings(), SENTINEL + "q" * 30_000, [unit])
    assert source == "nosana"
    assert seen["query_len"] <= MAX_QUERY_CHARS


def test_sanitise_exception_strips_pydantic_input_preview():
    with pytest.raises(ValidationError) as excinfo:
        ScoreItem(unit_id="u1", text=SENTINEL + "B" * 25_000)
    raw = str(excinfo.value)
    assert SENTINEL in raw  # pydantic really does embed the input...
    cleaned = sanitise_exception(excinfo.value)
    assert SENTINEL not in cleaned  # ...and the sanitiser really removes it
    assert "string_too_long" in cleaned


async def test_validation_failure_reason_contains_no_context():
    try:
        ScoreItem(unit_id="u1", text=SENTINEL + "C" * 25_000)
    except ValidationError as exc:
        err = exc

    class BoomClient:
        async def score(self, query, units):
            raise err

    unit = make_unit(make_block(0, content="content"), unit_id="u0")
    settings = make_settings()
    scores, source, ms, reason = await obtain_relevance_scores(
        "query", [unit], BoomClient(), settings
    )
    assert source == "fallback"
    assert SENTINEL not in reason
    assert "ValidationError" in reason


@respx.mock
async def test_malicious_unit_id_in_response_cannot_carry_html():
    respx.post(f"{BASE}/score").respond(json={
        "model": "m", "device": "cpu", "latency_ms": 1.0,
        "scores": [
            {"unit_id": "<img src=x onerror=alert(1)>", "relevance": 0.5, "truncated": False}
        ],
    })

    unit = make_unit(make_block(0, content="content"), unit_id="u0")
    scores, source, ms, reason = await _score(make_settings(max_retries=0), "q", [unit])

    assert source == "fallback"  # response rejected, deterministic fallback used
    assert "<img" not in reason  # rejected by schema, never echoed into reason
