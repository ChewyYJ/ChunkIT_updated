from app.nosana.fallback import fallback_scores
from tests.conftest import make_block, make_unit


def _unit(i, text, content_type="text"):
    block = make_block(i, content=text)
    unit = make_unit(block, unit_id=f"u{i}", text=text)
    unit.content_type = content_type
    return unit


def test_deterministic_same_input_same_output():
    units = [
        _unit(0, "RetryManager throws timeout error after three attempts", "log"),
        _unit(1, "CSS build completed successfully"),
        _unit(2, "def retry(): pass", "code"),
    ]
    query = "fix the retry timeout bug"
    first = fallback_scores(query, units)
    second = fallback_scores(query, units)
    assert first == second


def test_relevant_unit_scores_higher_than_unrelated():
    relevant = _unit(0, "RetryManager exhausts retry attempts after a timeout error", "log")
    unrelated = _unit(1, "CSS compilation completed successfully")
    scores = fallback_scores("fix the authentication retry timeout bug", [relevant, unrelated])
    assert scores["u0"] > scores["u1"]


def test_scores_bounded_zero_to_one():
    units = [
        _unit(0, "error error error must never fail test bug fix required", "log"),
        _unit(1, "zzz"),
    ]
    scores = fallback_scores("error must never fail test bug fix required", units)
    for value in scores.values():
        assert 0.0 <= value <= 1.0


def test_empty_query_is_safe():
    units = [_unit(0, "some text here")]
    scores = fallback_scores("", units)
    assert 0.0 <= scores["u0"] <= 1.0


def test_every_unit_receives_a_score():
    units = [_unit(i, f"text {i}") for i in range(10)]
    scores = fallback_scores("text", units)
    assert set(scores) == {f"u{i}" for i in range(10)}
