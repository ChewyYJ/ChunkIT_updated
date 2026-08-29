import httpx
import pytest
import respx

from app.nosana.client import NosanaScorerClient
from tests.conftest import make_block, make_settings, make_unit

BASE = "http://scorer.test"


def _units(n: int):
    blocks = [make_block(i, content=f"content number {i} with words") for i in range(n)]
    return [make_unit(b, unit_id=f"u{i}") for i, b in enumerate(blocks)]


def _response_for(unit_ids, relevance=0.9):
    return {
        "model": "test-model",
        "device": "cpu",
        "latency_ms": 5.0,
        "scores": [
            {"unit_id": uid, "relevance": relevance, "truncated": False}
            for uid in unit_ids
        ],
    }


async def _score(settings, query, units):
    async with httpx.AsyncClient() as http_client:
        client = NosanaScorerClient(settings, http_client)
        return await client.score(query, units)


@respx.mock
async def test_success_with_out_of_order_ids():
    units = _units(3)
    shuffled = ["u2", "u0", "u1"]
    respx.post(f"{BASE}/score").respond(json=_response_for(shuffled))

    scores, latency = await _score(make_settings(), "find content", units)
    assert set(scores) == {"u0", "u1", "u2"}
    assert all(0.0 <= v <= 1.0 for v in scores.values())
    assert latency >= 0


@respx.mock
async def test_missing_id_raises():
    units = _units(3)
    respx.post(f"{BASE}/score").respond(json=_response_for(["u0", "u1"]))

    with pytest.raises(ValueError, match="mismatch"):
        await _score(make_settings(), "q", units)


@respx.mock
async def test_unknown_extra_id_raises():
    units = _units(2)
    respx.post(f"{BASE}/score").respond(json=_response_for(["u0", "u1", "ghost"]))

    with pytest.raises(ValueError, match="mismatch"):
        await _score(make_settings(), "q", units)


@respx.mock
async def test_duplicate_id_in_response_raises():
    units = _units(2)
    respx.post(f"{BASE}/score").respond(json=_response_for(["u0", "u0", "u1"]))

    with pytest.raises(ValueError, match="duplicate"):
        await _score(make_settings(), "q", units)


@respx.mock
async def test_timeout_retried_then_raises():
    units = _units(1)
    route = respx.post(f"{BASE}/score").mock(side_effect=httpx.ConnectTimeout("boom"))

    with pytest.raises(httpx.TimeoutException):
        await _score(make_settings(max_retries=1), "q", units)
    assert route.call_count == 2  # initial + 1 retry


@respx.mock
async def test_retryable_status_then_success():
    units = _units(1)
    route = respx.post(f"{BASE}/score")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=_response_for(["u0"])),
    ]

    scores, _ = await _score(make_settings(max_retries=1), "q", units)
    assert scores == {"u0": 0.9}
    assert route.call_count == 2


@respx.mock
async def test_auth_error_not_retried():
    units = _units(1)
    route = respx.post(f"{BASE}/score").respond(status_code=401)

    with pytest.raises(httpx.HTTPStatusError):
        await _score(make_settings(max_retries=3), "q", units)
    assert route.call_count == 1


@respx.mock
async def test_invalid_response_shape_raises():
    units = _units(1)
    respx.post(f"{BASE}/score").respond(json={"nonsense": True})

    with pytest.raises(Exception):
        await _score(make_settings(), "q", units)


@respx.mock
async def test_batching_splits_requests():
    units = _units(5)

    def responder(request):
        import json

        payload = json.loads(request.content)
        assert len(payload["items"]) <= 2
        ids = [item["unit_id"] for item in payload["items"]]
        return httpx.Response(200, json=_response_for(ids))

    route = respx.post(f"{BASE}/score").mock(side_effect=responder)

    scores, _ = await _score(make_settings(request_batch_size=2), "q", units)
    assert set(scores) == {f"u{i}" for i in range(5)}
    assert route.call_count == 3  # 2 + 2 + 1


@respx.mock
async def test_token_header_sent():
    units = _units(1)

    def responder(request):
        assert request.headers["X-Scorer-Token"] == "test-token"
        return httpx.Response(200, json=_response_for(["u0"]))

    respx.post(f"{BASE}/score").mock(side_effect=responder)
    await _score(make_settings(), "q", units)


async def test_validate_before_network():
    units = _units(1)
    with pytest.raises(ValueError, match="NOSANA_SCORER_URL"):
        await _score(make_settings(scorer_url=""), "q", units)
