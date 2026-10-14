"""M5-04: the score sheet is validated and the medians are computed only from operator scores."""

import pytest
from ai_agent_application.evaluation import ScoreError, parse_scores, summarise
from ai_agent_application.evaluation.rubric import RunScore


def sheet(*rows):
    return {"scores": list(rows)}


def row(scenario="Q-01", repeat=1, cycle=False, **criteria):
    return {"scenario": scenario, "repeat": repeat, "criteria": criteria, "cycle": cycle}


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({}, "SCORES_MISSING"),
        (sheet(row(continuity=0)), "SCORE_OUT_OF_RANGE"),
        (sheet(row(continuity=6)), "SCORE_OUT_OF_RANGE"),
        (sheet(row(continuity=4.5)), "SCORE_NOT_INTEGER"),
        (sheet(row(continuity=True)), "SCORE_NOT_INTEGER"),
        (sheet(row(charm=3)), "CRITERION_UNKNOWN"),
        (sheet(row(), row()), "ROW_DUPLICATE"),
        (sheet({"scenario": "Q-01", "repeat": "1"}), "ROW_ID_INVALID"),
        (sheet(row(cycle="yes")), "CYCLE_NOT_BOOLEAN"),
        (sheet("text"), "ROW_NOT_OBJECT"),
    ],
)
def test_bad_sheet_is_rejected_with_a_code(data, code):
    if code == "CYCLE_NOT_BOOLEAN":
        data = sheet({**row(continuity=4), "cycle": "yes"})
    with pytest.raises(ScoreError, match=code):
        parse_scores(data)


def test_null_means_not_applicable_and_is_not_counted():
    scores = parse_scores(sheet(row(continuity=4, memory=None)))
    assert scores[0].values == {"continuity": 4}


def expected(n=2):
    return [("Q-01", i) for i in range(1, n + 1)]


def test_no_verdict_until_every_expected_run_is_scored():
    part = summarise([RunScore("Q-01", 1, {"continuity": 5})], expected())
    assert part.meets_target is None and not part.complete and part.overall_median == 5.0


def test_median_target_and_per_criterion():
    scores = [
        RunScore("Q-01", 1, {"continuity": 4, "memory": 5}),
        RunScore("Q-01", 2, {"continuity": 3, "memory": 4}),
    ]
    result = summarise(scores, expected())
    assert result.complete and result.overall_median == 4.0 and result.meets_target is True
    assert result.per_criterion == {"continuity": 3.5, "memory": 4.5}
    low = summarise([RunScore("Q-01", i, {"continuity": 3}) for i in (1, 2)], expected())
    assert low.overall_median == 3.0 and low.meets_target is False


def test_stable_cycle_needs_two_repeats_and_fails_the_target():
    one = [RunScore("Q-01", 1, {"memory": 5}, cycle=True), RunScore("Q-01", 2, {"memory": 5})]
    assert summarise(one, expected()).stable_cycles == ()
    two = [RunScore("Q-01", i, {"memory": 5}, cycle=True) for i in (1, 2)]
    result = summarise(two, expected())
    assert result.stable_cycles == ("Q-01",) and result.meets_target is False


def test_unexpected_rows_are_ignored_and_focusless_scenarios_need_no_score():
    scores = [RunScore("Q-99", 1, {"memory": 1}), RunScore("Q-01", 1, {"memory": 5})]
    result = summarise(scores, [("Q-01", 1), ("Q-02", 1)], {"Q-01": ("memory",), "Q-02": ()})
    assert result.complete and result.overall_median == 5.0 and result.scored_runs == 1


def test_target_uses_the_median_not_the_mean():
    # mean 3.67 would miss the target; the median 5 meets it (the doc22 rule is a median)
    scores = [
        RunScore("Q-01", 1, {"memory": 1}),
        RunScore("Q-01", 2, {"memory": 5, "continuity": 5}),
    ]
    result = summarise(scores, expected())
    assert result.overall_median == 5.0 and result.meets_target is True
