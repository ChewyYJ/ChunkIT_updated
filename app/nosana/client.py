from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import httpx

from app.nosana.config import NosanaSettings
from app.nosana.schemas import (
    MAX_QUERY_CHARS,
    MAX_UNIT_CHARS,
    ScoreItem,
    ScoreRequest,
    ScoreResponse,
)
from shared.schemas import ContextUnit

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class NosanaScorerClient:
    def __init__(self, settings: NosanaSettings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def score(
        self, query: str, units: Sequence[ContextUnit]
    ) -> tuple[dict[str, float], float]:
        self.settings.validate_for_remote()
        # Clamp to the scorer's wire limits so degenerate inputs degrade the
        # score, never the whole request (and never leak into error text).
        query = query[:MAX_QUERY_CHARS] if query.strip() else "(no objective provided)"
        all_scores: dict[str, float] = {}
        total_ms = 0.0

        for start in range(0, len(units), self.settings.request_batch_size):
            batch = units[start : start + self.settings.request_batch_size]
            scores, elapsed = await self._score_batch(query, batch)
            overlap = all_scores.keys() & scores.keys()
            if overlap:
                raise ValueError(f"duplicate scorer IDs across batches: {sorted(overlap)}")
            all_scores.update(scores)
            total_ms += elapsed

        expected = {unit.unit_id for unit in units}
        if set(all_scores) != expected:
            missing = sorted(expected - set(all_scores))
            extra = sorted(set(all_scores) - expected)
            raise ValueError(f"scorer ID mismatch; missing={missing}, extra={extra}")
        return all_scores, total_ms

    async def _score_batch(
        self, query: str, units: Sequence[ContextUnit]
    ) -> tuple[dict[str, float], float]:
        # Blank-text units cannot be sent to the scorer; they carry no signal,
        # so score them zero locally instead of failing the batch.
        scorable = [u for u in units if u.text.strip()]
        blank_scores = {u.unit_id: 0.0 for u in units if not u.text.strip()}
        if not scorable:
            return blank_scores, 0.0

        payload = ScoreRequest(
            query=query,
            items=[
                ScoreItem(
                    unit_id=u.unit_id,
                    text=u.text[:MAX_UNIT_CHARS],
                    content_type=u.content_type,
                )
                for u in scorable
            ],
        )
        last_error: Exception | None = None

        for attempt in range(self.settings.max_retries + 1):
            started = time.perf_counter()
            try:
                response = await self.client.post(
                    f"{self.settings.scorer_url}/score",
                    headers={"X-Scorer-Token": self.settings.scorer_token},
                    json=payload.model_dump(),
                    timeout=self.settings.timeout_seconds,
                )
                if response.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        "retryable scorer response",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                parsed = ScoreResponse.model_validate(response.json())
                elapsed = (time.perf_counter() - started) * 1000
                batch_scores: dict[str, float] = dict(blank_scores)
                for item in parsed.scores:
                    if item.unit_id in batch_scores:
                        raise ValueError(f"duplicate scorer ID in response: {item.unit_id}")
                    batch_scores[item.unit_id] = item.relevance
                return batch_scores, elapsed
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in RETRYABLE_STATUS
                ):
                    break
                if attempt < self.settings.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))

        assert last_error is not None
        raise last_error
