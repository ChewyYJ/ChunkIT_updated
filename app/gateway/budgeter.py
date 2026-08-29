from app.gateway.utils import estimate_tokens
from shared.schemas import ContextBlock, ContextUnit, ScoredUnit, SelectionPlan

# Estimated cost of one "[CONTEXT OMITTED ...]" marker; charged once per
# omitted block so many omissions cannot silently blow past the target.
MARKER_TOKEN_COST = 24


def build_selection_plan(
    blocks: list[ContextBlock],
    units_by_block: dict[str, list[ContextUnit]],
    scored: list[ScoredUnit],
    target_tokens: int,
) -> SelectionPlan:
    """Fit context into the token budget.

    Three tiers, and the accounting matches what the assembler forwards:
    - keep: pinned blocks, forwarded verbatim -> charged ORIGINAL tokens
    - compact: kept non-pinned units, forwarded as compressed unit text ->
      charged compressed unit tokens
    - omit: replaced by a marker -> charged one marker per block
    """
    priority_by_id = {s.unit_id: s.final_priority for s in scored}
    message_index_by_block = {b.block_id: b.message_index for b in blocks}

    keep: list[str] = []
    compact: list[str] = []
    omit: list[str] = []
    estimated_tokens = 0
    non_pinned_units: list[ContextUnit] = []

    for block in blocks:
        units = units_by_block.get(block.block_id, [])
        if block.pinned:
            # Pinned context is always kept verbatim, even over budget; the
            # caller reports over-budget rather than dropping a system
            # instruction or the current turn.
            estimated_tokens += estimate_tokens(block.content)
            keep.extend(u.unit_id for u in units)
        else:
            non_pinned_units.extend(units)

    # Deterministic ordering: priority desc, then newest message first,
    # then unit_id asc so equal-priority ties never depend on input order.
    non_pinned_units.sort(
        key=lambda u: (
            -priority_by_id.get(u.unit_id, 0.0),
            -message_index_by_block.get(u.block_id, 0),
            u.unit_id,
        )
    )

    marker_charged_blocks: set[str] = set()
    for u in non_pinned_units:
        if estimated_tokens + u.tokens <= target_tokens:
            compact.append(u.unit_id)
            estimated_tokens += u.tokens
        else:
            omit.append(u.unit_id)
            if u.block_id not in marker_charged_blocks:
                marker_charged_blocks.add(u.block_id)
                estimated_tokens += MARKER_TOKEN_COST

    return SelectionPlan(
        keep=keep,
        compact=compact,
        omit=omit,
        target_tokens=target_tokens,
        estimated_tokens=estimated_tokens,
    )
