import pytest

from app.scoring.priority import ROLE_IMPORTANCE, add_final_priorities
from tests.conftest import make_block, make_unit


def test_exact_weighted_formula():
    # message_index=3 of 4 messages -> recency = 4/4 = 1.0
    block = make_block(3, role="user")
    unit = make_unit(block, unit_id="u1", structural_priority=0.5)
    scored = add_final_priorities([unit], {"u1": 0.8}, {block.block_id: block}, message_count=4)

    expected = 0.65 * 0.8 + 0.15 * 1.0 + 0.10 * 0.5 + 0.10 * 1.0
    assert scored[0].relevance == 0.8
    assert scored[0].final_priority == pytest.approx(expected)


def test_recency_scales_with_message_index():
    early = make_block(0, role="assistant")
    late = make_block(9, role="assistant")
    units = [make_unit(early, unit_id="ue"), make_unit(late, unit_id="ul")]
    blocks = {b.block_id: b for b in (early, late)}
    scored = {s.unit_id: s for s in add_final_priorities(units, {"ue": 0.5, "ul": 0.5}, blocks, 10)}

    assert scored["ul"].final_priority > scored["ue"].final_priority
    diff = scored["ul"].final_priority - scored["ue"].final_priority
    assert diff == pytest.approx(0.15 * (10 - 1) / 10)


def test_pinned_block_gets_full_priority_regardless_of_relevance():
    block = make_block(0, role="system", pinned=True)
    unit = make_unit(block, unit_id="u1")
    scored = add_final_priorities([unit], {"u1": 0.01}, {block.block_id: block}, 5)
    assert scored[0].relevance == 1.0
    assert scored[0].final_priority == 1.0


def test_missing_relevance_defaults_to_zero():
    block = make_block(0, role="assistant")
    unit = make_unit(block, unit_id="u1", structural_priority=0.0)
    scored = add_final_priorities([unit], {}, {block.block_id: block}, 1)
    expected = 0.65 * 0.0 + 0.15 * 1.0 + 0.10 * 0.0 + 0.10 * 0.6
    assert scored[0].final_priority == pytest.approx(expected)


def test_unknown_role_uses_default_importance():
    block = make_block(0, role="narrator")
    unit = make_unit(block, unit_id="u1", structural_priority=0.0)
    scored = add_final_priorities([unit], {"u1": 0.0}, {block.block_id: block}, 1)
    expected = 0.15 * 1.0 + 0.10 * 0.5
    assert scored[0].final_priority == pytest.approx(expected)


def test_structural_priority_clamped():
    block = make_block(0, role="user")
    unit = make_unit(block, unit_id="u1", structural_priority=5.0)
    scored = add_final_priorities([unit], {"u1": 1.0}, {block.block_id: block}, 1)
    expected = 0.65 * 1.0 + 0.15 * 1.0 + 0.10 * 1.0 + 0.10 * 1.0
    assert scored[0].final_priority == pytest.approx(min(1.0, expected))


def test_final_priority_always_bounded():
    block = make_block(0, role="user")
    unit = make_unit(block, unit_id="u1", structural_priority=1.0)
    scored = add_final_priorities([unit], {"u1": 1.0}, {block.block_id: block}, 1)
    assert 0.0 <= scored[0].final_priority <= 1.0


def test_role_importance_table():
    assert ROLE_IMPORTANCE["user"] == 1.0
    assert ROLE_IMPORTANCE["tool"] == 0.8
    assert ROLE_IMPORTANCE["assistant"] == 0.6
    assert ROLE_IMPORTANCE["system"] == 1.0


def test_every_unit_scored_exactly_once():
    blocks = [make_block(i) for i in range(6)]
    units = [make_unit(b, unit_id=f"u{i}") for i, b in enumerate(blocks)]
    scored = add_final_priorities(
        units, {f"u{i}": 0.5 for i in range(6)}, {b.block_id: b for b in blocks}, 6
    )
    assert [s.unit_id for s in scored] == [f"u{i}" for i in range(6)]
