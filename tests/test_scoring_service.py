import httpx
import pytest
import respx

from app.nosana.client import NosanaScorerClient
from app.scoring.service import extract_query, obtain_relevance_scores
from tests.conftest import make_block, make_settings, make_unit


def _candidates(n=2):
    blocks = [make_block(i, content=f"candidate text {i}") for i in range(n)]
    return [make_unit(b, unit_id=f"u{i}") for i, b in enumerate(blocks)]


async def _client(settings):
    return NosanaScorerClient(settings, httpx.AsyncClient())


async def test_no_candidates_short_circuits():
    settings = make_settings()
    scores, source, ms, reason = await obtain_relevance_scores("q", [], None, settings)
    assert scores == {}
    assert source == "fallback"
    assert ms == 0.0


async def test_disabled_uses_fallback_with_reason():
    settings = make_settings(enabled=False)
    scores, source, ms, reason = await obtain_relevance_scores(
        "candidate text", _candidates(), None, settings
    )
    assert source == "fallback"
    assert reason == "NOSANA_ENABLED=false"
    assert set(scores) == {"u0", "u1"}


async def test_misconfigured_url_falls_back_without_network():
    settings = make_settings(scorer_url="")  # enabled but not configured
    client = await _client(settings)
    scores, source, ms, reason = await obtain_relevance_scores(
        "candidate text", _candidates(), client, settings
    )
    assert source == "fallback"
    assert "NOSANA_SCORER_URL" in reason
    assert set(scores) == {"u0", "u1"}


@respx.mock
async def test_remote_failure_falls_back_with_reason():
    respx.post("http://scorer.test/score").mock(side_effect=httpx.ConnectError("down"))
    settings = make_settings(max_retries=0)
    client = await _client(settings)
    scores, source, ms, reason = await obtain_relevance_scores(
        "candidate text", _candidates(), client, settings
    )
    assert source == "fallback"
    assert "ConnectError" in reason
    assert set(scores) == {"u0", "u1"}


@respx.mock
async def test_remote_failure_raises_when_fallback_disabled():
    respx.post("http://scorer.test/score").mock(side_effect=httpx.ConnectError("down"))
    settings = make_settings(max_retries=0, fallback_enabled=False)
    client = await _client(settings)
    with pytest.raises(httpx.ConnectError):
        await obtain_relevance_scores("q", _candidates(), client, settings)


@respx.mock
async def test_remote_success_reports_nosana_source():
    units = _candidates()
    respx.post("http://scorer.test/score").respond(
        json={
            "model": "m", "device": "cuda", "latency_ms": 3.0,
            "scores": [
                {"unit_id": "u0", "relevance": 0.9, "truncated": False},
                {"unit_id": "u1", "relevance": 0.1, "truncated": False},
            ],
        }
    )
    settings = make_settings()
    client = await _client(settings)
    scores, source, ms, reason = await obtain_relevance_scores("q", units, client, settings)
    assert source == "nosana"
    assert reason is None
    assert scores == {"u0": 0.9, "u1": 0.1}
    assert ms > 0


def test_extract_query_prefers_last_user_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first ask"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "the real objective"},
        {"role": "assistant", "content": "working on it"},
    ]
    assert extract_query(messages) == "the real objective"


def test_extract_query_falls_back_to_last_message():
    messages = [{"role": "assistant", "content": "only assistant"}]
    assert extract_query(messages) == "only assistant"
    assert extract_query([]) == ""
