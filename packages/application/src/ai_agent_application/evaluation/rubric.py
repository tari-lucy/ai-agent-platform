"""doc22 rubric: six criteria scored 1-5 by the operator; target median >= 4, no stable cycle.

The platform never scores quality. This module only validates what the operator entered and
computes the medians, so the summary cannot be produced without the operator's scores.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

CRITERIA: tuple[tuple[str, str], ...] = (
    ("continuity", "Непрерывность характера"),
    ("own_choice", "Собственный выбор"),
    ("humor", "Уместный юмор"),
    ("memory", "Конкретность памяти"),
    ("variety", "Вариативность возможностей"),
    ("development", "Проявление развития"),
)
CRITERION_IDS = frozenset(key for key, _ in CRITERIA)
MIN_SCORE, MAX_SCORE = 1, 5
TARGET_MEDIAN = 4.0
# a repeated cycle is "stable" when seen in at least this many repeats of one scenario
STABLE_CYCLE_REPEATS = 2


class ScoreError(ValueError):
    """A score sheet entry is not usable; the message carries codes, never operator notes."""


@dataclass(frozen=True)
class RunScore:
    scenario_id: str
    repeat: int
    values: Mapping[str, int]
    cycle: bool = False
    note: str = ""


@dataclass(frozen=True)
class Summary:
    scored_runs: int
    expected_runs: int
    scores_given: int
    overall_median: float | None
    per_criterion: Mapping[str, float]
    per_scenario: Mapping[str, float]
    stable_cycles: tuple[str, ...]
    complete: bool
    meets_target: bool | None  # None until every expected run has been scored

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored_runs": self.scored_runs,
            "expected_runs": self.expected_runs,
            "scores_given": self.scores_given,
            "overall_median": self.overall_median,
            "per_criterion": dict(self.per_criterion),
            "per_scenario": dict(self.per_scenario),
            "stable_cycles": list(self.stable_cycles),
            "complete": self.complete,
            "meets_target": self.meets_target,
        }


def parse_scores(sheet: Mapping[str, Any]) -> list[RunScore]:
    """Read the operator's JSON sheet: ``{"scores": [{"scenario", "repeat", "criteria", ...}]}``."""
    rows = sheet.get("scores")
    if not isinstance(rows, list):
        raise ScoreError("SCORES_MISSING")
    result: list[RunScore] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ScoreError("ROW_NOT_OBJECT")
        scenario, repeat = row.get("scenario"), row.get("repeat")
        if not isinstance(scenario, str) or not isinstance(repeat, int) or isinstance(repeat, bool):
            raise ScoreError("ROW_ID_INVALID")
        if (scenario, repeat) in seen:
            raise ScoreError("ROW_DUPLICATE")
        seen.add((scenario, repeat))
        criteria = row.get("criteria", {})
        if not isinstance(criteria, Mapping):
            raise ScoreError("CRITERIA_NOT_OBJECT")
        values: dict[str, int] = {}
        for key, value in criteria.items():
            if key not in CRITERION_IDS:
                raise ScoreError("CRITERION_UNKNOWN")
            if value is None:  # "n/a": the criterion did not apply to this run
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ScoreError("SCORE_NOT_INTEGER")
            if not MIN_SCORE <= value <= MAX_SCORE:
                raise ScoreError("SCORE_OUT_OF_RANGE")
            values[key] = value
        cycle = row.get("cycle", False)
        if not isinstance(cycle, bool):
            raise ScoreError("CYCLE_NOT_BOOLEAN")
        note = row.get("note", "")
        result.append(
            RunScore(scenario, repeat, values, cycle, note if isinstance(note, str) else "")
        )
    return result


def summarise(
    scores: Iterable[RunScore],
    expected: Sequence[tuple[str, int]],
    focus: Mapping[str, Sequence[str]] | None = None,
) -> Summary:
    """Medians of the operator's scores; ``meets_target`` stays ``None`` until all are in."""
    rows = list(scores)
    wanted = set(expected)
    known = [row for row in rows if (row.scenario_id, row.repeat) in wanted]
    scored = {(r.scenario_id, r.repeat) for r in known if r.values}
    flat = [value for row in known for value in row.values.values()]
    per_criterion: dict[str, float] = {}
    for key, _ in CRITERIA:
        found = [row.values[key] for row in known if key in row.values]
        if found:
            per_criterion[key] = float(median(found))
    per_scenario: dict[str, float] = {}
    for scenario in sorted({r.scenario_id for r in known}):
        found = [v for r in known if r.scenario_id == scenario for v in r.values.values()]
        if found:
            per_scenario[scenario] = float(median(found))
    cycles = sorted(
        scenario
        for scenario in {r.scenario_id for r in known}
        if sum(1 for r in known if r.scenario_id == scenario and r.cycle) >= STABLE_CYCLE_REPEATS
    )
    # a run of a scenario without scored criteria (platform-only) needs no operator score
    required = {key for key in wanted if focus is None or focus.get(key[0])}
    complete = bool(required) and scored >= required
    overall = float(median(flat)) if flat else None
    meets = None
    if complete and overall is not None:
        meets = overall >= TARGET_MEDIAN and not cycles
    return Summary(
        scored_runs=len(scored),
        expected_runs=len(wanted),
        scores_given=len(flat),
        overall_median=overall,
        per_criterion=per_criterion,
        per_scenario=per_scenario,
        stable_cycles=tuple(cycles),
        complete=complete,
        meets_target=meets,
    )
