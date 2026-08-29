from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Optional

from pydantic import ValidationError

from app.nosana.client import NosanaScorerClient
from app.nosana.config import NosanaSettings
from app.nosana.fallback import fallback_scores
from shared.schemas import ContextUnit


def sanitise_exception(exc: BaseException) -> str:
    """Exception text safe for logs/debug output.

    pydantic ValidationError messages embed a preview of the offending input
    value — which here can be raw conversation context. Reduce them to
    location + error type only; every other exception keeps type + message.
    """
    if isinstance(exc, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['type']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        return f"ValidationError: {details}"
    return f"{type(exc).__name__}: {exc}"


async def obtain_relevance_scores(
    query: str,
    candidates: Sequence[ContextUnit],
    client: NosanaScorerClient,
    settings: NosanaSettings,
) -> tuple[dict[str, float], Literal["nosana", "fallback"], float, Optional[str]]:
    """Score non-pinned candidate units.

    Tries the live Nosana scorer first, then the deterministic local fallback.
    A Nosana outage is NOT a compression-pipeline failure: it returns fallback
    scores with a recorded reason instead of raising into the gateway-wide
    fail-open handler. Exceptions escape only when the fallback is disabled.
    """
    if not candidates:
        return {}, "fallback", 0.0, "no non-pinned units to score"

    if settings.enabled:
        try:
            scores, latency_ms = await client.score(query, candidates)
            return scores, "nosana", latency_ms, None
        except Exception as exc:
            if not settings.fallback_enabled:
                raise
            # Sanitised: never includes unit text (see sanitise_exception).
            reason = sanitise_exception(exc)
    else:
        reason = "NOSANA_ENABLED=false"

    return fallback_scores(query, candidates), "fallback", 0.0, reason


def extract_query(messages: list[dict]) -> str:
    """Latest user objective: the content of the most recent user message,
    falling back to the last message of any role."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            return content if isinstance(content, str) else str(content)
    if messages:
        content = messages[-1].get("content", "")
        return content if isinstance(content, str) else str(content)
    return ""
