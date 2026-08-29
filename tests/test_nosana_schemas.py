import pytest
from pydantic import ValidationError

from app.nosana.config import NosanaSettings
from app.nosana.schemas import ScoreItem, ScoreRequest, ScoreResponse, ScoreResult
from tests.conftest import make_settings


def _item(i: int = 0) -> dict:
    return {"unit_id": f"u{i}", "text": f"some context text {i}", "content_type": "text"}


def test_valid_request_parses():
    req = ScoreRequest(query="fix the bug", items=[_item()])
    assert req.items[0].unit_id == "u0"


def test_empty_query_rejected():
    with pytest.raises(ValidationError):
        ScoreRequest(query="", items=[_item()])


def test_empty_items_rejected():
    with pytest.raises(ValidationError):
        ScoreRequest(query="q", items=[])


def test_blank_text_rejected():
    with pytest.raises(ValidationError):
        ScoreItem(unit_id="u1", text="   ", content_type="text")


def test_over_128_items_rejected():
    with pytest.raises(ValidationError):
        ScoreRequest(query="q", items=[_item(i) for i in range(129)])


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ScoreRequest(query="q", items=[_item()], surprise=True)
    with pytest.raises(ValidationError):
        ScoreItem(unit_id="u1", text="ok", content_type="text", extra="no")


def test_relevance_bounds_enforced():
    with pytest.raises(ValidationError):
        ScoreResult(unit_id="u1", relevance=1.5)
    with pytest.raises(ValidationError):
        ScoreResult(unit_id="u1", relevance=-0.1)


def test_score_response_shape():
    resp = ScoreResponse(
        model="m", device="cpu", latency_ms=1.0,
        scores=[{"unit_id": "u1", "relevance": 0.5, "truncated": False}],
    )
    assert resp.scores[0].relevance == 0.5


def test_settings_validate_for_remote_requires_url_and_token():
    with pytest.raises(ValueError, match="NOSANA_SCORER_URL"):
        make_settings(scorer_url="").validate_for_remote()
    with pytest.raises(ValueError, match="NOSANA_SCORER_TOKEN"):
        make_settings(scorer_token="").validate_for_remote()
    make_settings().validate_for_remote()  # valid config passes


def test_settings_from_env_defaults(monkeypatch):
    for name in (
        "NOSANA_ENABLED", "NOSANA_SCORER_URL", "NOSANA_SCORER_TOKEN",
        "NOSANA_TIMEOUT_SECONDS", "NOSANA_MAX_RETRIES",
        "NOSANA_REQUEST_BATCH_SIZE", "NOSANA_FALLBACK_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    s = NosanaSettings.from_env()
    assert s.enabled is True
    assert s.fallback_enabled is True
    assert s.timeout_seconds == 5.0
    assert s.max_retries == 2
    assert s.request_batch_size == 128


def test_settings_url_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("NOSANA_SCORER_URL", "http://x.test/")
    assert NosanaSettings.from_env().scorer_url == "http://x.test"
