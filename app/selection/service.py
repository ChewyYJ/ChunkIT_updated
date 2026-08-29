from __future__ import annotations

from app.gateway.budgeter import build_selection_plan
from app.nosana.client import NosanaScorerClient
from app.nosana.config import NosanaSettings
from app.nosana.schemas import RankAndSelectOutcome
from app.scoring.priority import add_final_priorities
from app.scoring.service import obtain_relevance_scores
from shared.schemas import ContextBlock, ContextUnit


async def rank_and_select(
    *,
    query: str,
    blocks: list[ContextBlock],
    units: list[ContextUnit],
    target_tokens: int,
    nosana_client: NosanaScorerClient,
    settings: NosanaSettings,
) -> RankAndSelectOutcome:
    """Return Person 1-compatible SelectionPlan plus score/debug metadata.

    Guarantees:
    - every unit receives exactly one ScoredUnit;
    - pinned blocks are never dropped by this module;
    - Nosana failure uses the deterministic fallback when enabled;
    - unit_id and block_id are never rewritten;
    - no raw context is logged;
    - exceptions escape only when both remote and fallback paths fail, or when
      a contract/integrity error makes safe selection impossible.
    """
    blocks_by_id = {block.block_id: block for block in blocks}

    missing_blocks = {u.block_id for u in units} - blocks_by_id.keys()
    if missing_blocks:
        raise ValueError(f"units reference unknown block_ids: {sorted(missing_blocks)}")

    # Pinned context bypasses Nosana entirely — only score compressible units.
    candidates = [u for u in units if not blocks_by_id[u.block_id].pinned]

    relevance, source, nosana_ms, fallback_reason = await obtain_relevance_scores(
        query, candidates, nosana_client, settings
    )

    scored_units = add_final_priorities(
        units, relevance, blocks_by_id, message_count=len(blocks)
    )

    units_by_block: dict[str, list[ContextUnit]] = {b.block_id: [] for b in blocks}
    for unit in units:
        units_by_block[unit.block_id].append(unit)

    plan = build_selection_plan(blocks, units_by_block, scored_units, target_tokens)

    return RankAndSelectOutcome(
        plan=plan,
        scored_units=scored_units,
        score_source=source,
        nosana_ms=nosana_ms,
        fallback_reason=fallback_reason,
    )
