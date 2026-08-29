from __future__ import annotations

from collections.abc import Mapping

from shared.schemas import ContextBlock, ContextUnit, ScoredUnit

ROLE_IMPORTANCE = {
    "user": 1.0,
    "tool": 0.8,
    "assistant": 0.6,
    "system": 1.0,
    "developer": 1.0,
}

RELEVANCE_WEIGHT = 0.65
RECENCY_WEIGHT = 0.15
STRUCTURAL_WEIGHT = 0.10
ROLE_WEIGHT = 0.10


def add_final_priorities(
    units: list[ContextUnit],
    relevance_by_id: dict[str, float],
    blocks_by_id: Mapping[str, ContextBlock],
    message_count: int,
) -> list[ScoredUnit]:
    scored: list[ScoredUnit] = []
    denom = max(1, message_count)

    for unit in units:
        block = blocks_by_id[unit.block_id]
        relevance = 1.0 if block.pinned else relevance_by_id.get(unit.unit_id, 0.0)
        recency = max(0.0, min(1.0, (block.message_index + 1) / denom))
        structural = max(0.0, min(1.0, unit.structural_priority))
        role = ROLE_IMPORTANCE.get(block.role, 0.5)

        final = 1.0 if block.pinned else (
            RELEVANCE_WEIGHT * relevance
            + RECENCY_WEIGHT * recency
            + STRUCTURAL_WEIGHT * structural
            + ROLE_WEIGHT * role
        )
        scored.append(
            ScoredUnit(
                unit_id=unit.unit_id,
                relevance=relevance,
                final_priority=max(0.0, min(1.0, final)),
            )
        )
    return scored
