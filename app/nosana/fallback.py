from __future__ import annotations

import re
from collections.abc import Iterable

from shared.schemas import ContextUnit

TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
IMPORTANT = {
    "error", "exception", "failed", "failure", "warning", "constraint",
    "must", "never", "required", "todo", "fix", "bug", "test", "traceback",
}


def _terms(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text)}


def fallback_scores(query: str, units: Iterable[ContextUnit]) -> dict[str, float]:
    """Deterministic lexical scorer used when the remote Nosana scorer is
    unreachable or disabled. Same input always produces the same scores."""
    q = _terms(query)
    q_denom = max(1, len(q))
    output: dict[str, float] = {}

    for unit in units:
        t = _terms(unit.text)
        overlap = q & t
        query_coverage = len(overlap) / q_denom
        local_density = len(overlap) / max(1, min(len(t), 50))
        important_bonus = 1.0 if IMPORTANT & t else 0.0
        type_bonus = 1.0 if unit.content_type in {"log", "code"} else 0.0

        score = (
            0.70 * query_coverage
            + 0.15 * local_density
            + 0.10 * important_bonus
            + 0.05 * type_bonus
        )
        output[unit.unit_id] = max(0.0, min(1.0, score))

    return output
