import pytest

from app.gateway.assembler import assemble_messages
from app.gateway.budgeter import MARKER_TOKEN_COST, build_selection_plan
from app.selection.service import rank_and_select
from shared.schemas import ScoredUnit
from tests.conftest import make_block, make_settings, make_unit


def _scored(unit_id, priority, relevance=0.5):
    return ScoredUnit(unit_id=unit_id, relevance=relevance, final_priority=priority)


def test_pinned_units_always_kept_even_over_budget():
    pinned = make_block(0, role="system", content="x" * 400, pinned=True)
    other = make_block(1, content="y" * 40)
    units_by_block = {
        pinned.block_id: [make_unit(pinned, unit_id="up", tokens=100)],
        other.block_id: [make_unit(other, unit_id="uo", tokens=10)],
    }
    plan = build_selection_plan(
        [pinned, other], units_by_block,
        [_scored("up", 1.0), _scored("uo", 0.9)],
        target_tokens=5,
    )
    assert "up" in plan.keep
    assert "uo" in plan.omit
    assert plan.estimated_tokens > plan.target_tokens  # reported, not hidden


def test_higher_priority_kept_first():
    blocks = [make_block(i) for i in range(3)]
    units_by_block = {
        b.block_id: [make_unit(b, unit_id=f"u{i}", tokens=10)]
        for i, b in enumerate(blocks)
    }
    plan = build_selection_plan(
        blocks, units_by_block,
        [_scored("u0", 0.2), _scored("u1", 0.9), _scored("u2", 0.5)],
        target_tokens=20,
    )
    assert set(plan.compact) == {"u1", "u2"}  # non-pinned kept -> compact tier
    assert plan.omit == ["u0"]


def test_tiebreak_prefers_newer_message_then_unit_id():
    blocks = [make_block(i) for i in range(3)]
    units_by_block = {
        b.block_id: [make_unit(b, unit_id=f"u{i}", tokens=10)]
        for i, b in enumerate(blocks)
    }
    scored = [_scored("u0", 0.5), _scored("u1", 0.5), _scored("u2", 0.5)]

    plan = build_selection_plan(blocks, units_by_block, scored, target_tokens=10)
    # Equal priority: newest message (msg_2) wins deterministically.
    assert plan.compact == ["u2"]
    assert set(plan.omit) == {"u0", "u1"}

    # Order of the scored list must not change the outcome.
    plan2 = build_selection_plan(blocks, units_by_block, list(reversed(scored)), target_tokens=10)
    assert plan2.compact == plan.compact
    assert plan2.omit == plan.omit


def test_marker_overhead_accounted_once_per_omitted_block():
    blocks = [make_block(i) for i in range(2)]
    units_by_block = {
        b.block_id: [make_unit(b, unit_id=f"u{i}", tokens=100)]
        for i, b in enumerate(blocks)
    }
    plan = build_selection_plan(
        blocks, units_by_block,
        [_scored("u0", 0.9), _scored("u1", 0.8)],
        target_tokens=100,
    )
    # u0 (priority 0.9) fills the whole budget; u1 is omitted and its block
    # is charged exactly one marker.
    assert plan.compact == ["u0"]
    assert plan.omit == ["u1"]
    assert plan.estimated_tokens == 100 + MARKER_TOKEN_COST


def test_assembler_restores_message_order():
    blocks = [make_block(i, content=f"message number {i}") for i in range(4)]
    units_by_block = {
        b.block_id: [make_unit(b, unit_id=f"u{i}", tokens=5)]
        for i, b in enumerate(blocks)
    }
    plan = build_selection_plan(
        blocks, units_by_block,
        [_scored(f"u{i}", p) for i, p in enumerate([0.1, 0.9, 0.2, 0.8])],
        target_tokens=10,
    )
    assembled = assemble_messages(blocks, units_by_block, plan)
    # Message order preserved regardless of ranking order.
    assert len(assembled) == 4
    kept_contents = [m["content"] for m in assembled]
    assert kept_contents[1] == "message number 1"
    assert kept_contents[3] == "message number 3"
    assert kept_contents[0].startswith("[CONTEXT OMITTED")
    assert kept_contents[2].startswith("[CONTEXT OMITTED")


async def test_rank_and_select_end_to_end_with_fallback():
    settings = make_settings(enabled=False)  # force deterministic fallback, no network
    blocks = [
        make_block(0, role="system", content="You are helpful.", pinned=True),
        make_block(1, content="RetryManager timeout error in auth retry logic " * 5),
        make_block(2, content="CSS build completed successfully " * 5),
        make_block(3, content="Fix the retry timeout bug", pinned=True),
    ]
    units = [make_unit(b, unit_id=f"u{i}") for i, b in enumerate(blocks)]

    outcome = await rank_and_select(
        query="Fix the retry timeout bug",
        blocks=blocks,
        units=units,
        target_tokens=60,
        nosana_client=None,  # never touched when disabled
        settings=settings,
    )
    assert outcome.score_source == "fallback"
    assert outcome.fallback_reason == "NOSANA_ENABLED=false"
    assert len(outcome.scored_units) == len(units)
    assert "u0" in outcome.plan.keep and "u3" in outcome.plan.keep  # pinned survive
    scored = {s.unit_id: s for s in outcome.scored_units}
    assert scored["u1"].relevance > scored["u2"].relevance  # relevant beats chatter


async def test_rank_and_select_rejects_unknown_block_ids():
    block = make_block(0)
    orphan = make_unit(block, unit_id="u_orphan")
    orphan.block_id = "msg_999"
    with pytest.raises(ValueError, match="unknown block_ids"):
        await rank_and_select(
            query="q", blocks=[block], units=[orphan], target_tokens=100,
            nosana_client=None, settings=make_settings(enabled=False),
        )
