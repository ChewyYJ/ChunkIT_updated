"""Optional live smoke test against the deployed Nosana scorer.

Skipped unless RUN_LIVE_NOSANA_TESTS=1 and the scorer URL/token are set —
never runs in ordinary CI.
"""

import os

import httpx
import pytest

RUN_LIVE = os.getenv("RUN_LIVE_NOSANA_TESTS", "0") == "1"
SCORER_URL = os.getenv("NOSANA_SCORER_URL", "").rstrip("/")
SCORER_TOKEN = os.getenv("NOSANA_SCORER_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (RUN_LIVE and SCORER_URL and SCORER_TOKEN),
    reason="live Nosana test disabled (set RUN_LIVE_NOSANA_TESTS=1, NOSANA_SCORER_URL, NOSANA_SCORER_TOKEN)",
)


def test_health_is_ready():
    resp = httpx.get(f"{SCORER_URL}/health", timeout=15)
    resp.raise_for_status()
    body = resp.json()
    assert body["status"] == "ready", f"scorer not ready: {body}"


def test_relevant_unit_outscores_unrelated():
    resp = httpx.post(
        f"{SCORER_URL}/score",
        headers={"X-Scorer-Token": SCORER_TOKEN},
        json={
            "query": "fix the authentication retry timeout bug",
            "items": [
                {
                    "unit_id": "relevant",
                    "text": "RetryManager exhausts retries after a timeout in the auth flow",
                    "content_type": "log",
                },
                {
                    "unit_id": "unrelated",
                    "text": "CSS compilation completed successfully with no warnings",
                    "content_type": "log",
                },
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    scores = {s["unit_id"]: s["relevance"] for s in resp.json()["scores"]}
    assert scores["relevant"] > scores["unrelated"]
    assert all(0.0 <= v <= 1.0 for v in scores.values())
