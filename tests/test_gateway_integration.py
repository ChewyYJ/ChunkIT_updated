import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.gateway.routes as routes
import app.session.store as store
from app.main import app

SCORER = "http://scorer.test"


def _long_messages():
    filler = "The CSS asset pipeline rebuilt successfully with no warnings emitted. " * 6
    relevant = "RetryManager exhausted retries after a timeout in the auth flow. " * 6
    return [
        {"role": "system", "content": "You are a precise coding assistant."},
        {"role": "user", "content": "Help me with my Python service."},
        {"role": "assistant", "content": filler},
        {"role": "user", "content": relevant},
        {"role": "assistant", "content": filler},
        {"role": "assistant", "content": filler},
        {"role": "assistant", "content": "Understood, I'll look at the retry logic."},
        {"role": "user", "content": "Fix the authentication retry timeout bug."},
    ]


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """TestClient with isolated DB, scorer env config, and a stubbed upstream."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("NOSANA_ENABLED", "true")
    monkeypatch.setenv("NOSANA_SCORER_URL", SCORER)
    monkeypatch.setenv("NOSANA_SCORER_TOKEN", "test-token")
    monkeypatch.setenv("NOSANA_MAX_RETRIES", "0")
    monkeypatch.setenv("NOSANA_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("NOSANA_FALLBACK_ENABLED", "true")

    calls = []

    async def fake_get_completion(messages, cache_key=None, session_id=None,
                                  include_retrieval_tool=True, client_tools=None,
                                  tool_choice=None, temperature=None, max_tokens=None):
        calls.append({
            "messages": messages,
            "cache_key": cache_key,
            "session_id": session_id,
            "include_retrieval_tool": include_retrieval_tool,
            "client_tools": client_tools,
        })
        return {
            "response": {
                "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                "model": "test-model",
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "stubbed upstream answer"},
                }],
                "usage": {"prompt_tokens": 42, "completion_tokens": 5, "total_tokens": 47},
            },
            "retrieval_rounds": 0,
            "cached_tokens": 0,
        }

    monkeypatch.setattr(routes, "get_completion", fake_get_completion)

    with TestClient(app) as client:
        yield client, calls


def _echo_scorer(relevance_by_hint=None):
    def responder(request):
        payload = json.loads(request.content)
        scores = []
        for item in payload["items"]:
            rel = 0.5
            if relevance_by_hint:
                for hint, value in relevance_by_hint.items():
                    if hint in item["text"]:
                        rel = value
            scores.append({"unit_id": item["unit_id"], "relevance": rel, "truncated": False})
        return httpx.Response(200, json={
            "model": "test-model", "device": "cpu", "latency_ms": 4.2, "scores": scores,
        })
    return responder


@respx.mock
def test_live_scoring_path_reports_nosana(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer(
        {"RetryManager": 0.95, "CSS asset pipeline": 0.05}
    ))

    # Budget large enough to keep the top-priority relevant block after the
    # pinned messages, but far too small for the filler blocks (which the
    # compact tier now forwards as deduped compressed text).
    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 150,
    })
    assert resp.status_code == 200
    debug = resp.json()["debug"]

    assert debug["score_source"] == "nosana"
    assert debug["fallback_reason"] is None
    assert debug["nosana_ms"] > 0
    assert debug["fail_open"] is False
    assert debug["omitted_blocks"] > 0
    assert debug["tokens_saved"] > 0
    assert 0 < debug["savings_percent"] <= 100
    assert debug["original_tokens"] > debug["forwarded_tokens"]
    assert debug["scored_units"], "sanitised unit scores must be present"
    for entry in debug["scored_units"]:
        assert set(entry) == {"unit_id", "relevance", "final_priority", "pinned"}

    # Relevant block outranked filler: it must have been kept.
    sent_text = " ".join(m["content"] for m in calls[-1]["messages"])
    assert "RetryManager exhausted retries" in sent_text


@respx.mock
def test_scorer_outage_falls_back_and_still_compresses(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=httpx.ConnectError("scorer down"))

    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 120,
    })
    assert resp.status_code == 200
    debug = resp.json()["debug"]

    assert debug["score_source"] == "fallback"
    assert "ConnectError" in debug["fallback_reason"]
    assert debug["fail_open"] is False
    assert debug["omitted_blocks"] > 0  # compression still happened
    assert calls, "upstream must still be called"


@respx.mock
def test_pipeline_crash_fails_open(gateway, monkeypatch):
    client, calls = gateway

    async def boom(**kwargs):
        raise RuntimeError("selection exploded")

    monkeypatch.setattr(routes, "rank_and_select", boom)

    messages = _long_messages()
    resp = client.post("/v1/chat/completions", json={
        "messages": messages, "target_tokens": 120,
    })
    assert resp.status_code == 200
    debug = resp.json()["debug"]

    assert debug["fail_open"] is True
    assert "selection exploded" in debug["fail_reason"]
    assert debug["sent"] == len(messages)  # originals forwarded untouched
    assert calls[-1]["messages"] == messages


@respx.mock
def test_passthrough_mode_skips_compression(gateway):
    client, calls = gateway
    route = respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    messages = _long_messages()
    resp = client.post("/v1/chat/completions", json={
        "messages": messages, "target_tokens": 120, "compression_enabled": False,
    })
    assert resp.status_code == 200
    debug = resp.json()["debug"]

    assert debug["score_source"] == "disabled"
    assert debug["compression_enabled"] is False
    assert debug["sent"] == len(messages)
    assert debug["tokens_saved"] == 0
    assert route.call_count == 0
    assert calls[-1]["messages"] == messages


@respx.mock
def test_retrieval_tool_only_advertised_when_omitting(gateway):
    client, calls = gateway
    respx.post(f"{SCORER}/score").mock(side_effect=_echo_scorer())

    # Huge budget: nothing omitted -> tool must not be advertised.
    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 100_000,
    })
    assert resp.json()["debug"]["omitted_blocks"] == 0
    assert calls[-1]["include_retrieval_tool"] is False

    # Tight budget: omission -> tool advertised.
    resp = client.post("/v1/chat/completions", json={
        "messages": _long_messages(), "target_tokens": 60,
    })
    assert resp.json()["debug"]["omitted_units"]  # some omission occurred
    assert calls[-1]["include_retrieval_tool"] is True


def test_request_validation(gateway):
    client, _ = gateway
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 422
    assert client.post("/v1/chat/completions", json={
        "messages": [{"role": "user"}],
    }).status_code == 422
    assert client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "target_tokens": 0,
    }).status_code == 422


def test_health_and_dashboard(gateway):
    client, _ = gateway
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert "daytona" in health  # Person 2 worker-pool status is reported too
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "ChunkIT" in page.text
