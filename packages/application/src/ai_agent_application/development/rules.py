"""Pure development rules: what a proposed change may do to one property. No storage, no model."""

from dataclasses import dataclass
from typing import Literal

from ai_agent_application.persona.schemas import DevelopmentBound, PropertyDefinition

type Route = Literal["autonomous", "operator"]

NUMERIC_EPS = 1e-9


@dataclass(frozen=True)
class Assessment:
    route: Route
    reasons: tuple[str, ...]  # why it is not autonomous (empty for autonomous)
    errors: tuple[str, ...]  # why it is invalid at all


def value_error(definition: PropertyDefinition, value: float | str) -> str | None:
    if definition.kind == "scale":
        if isinstance(value, str) or isinstance(value, bool):
            return "VALUE_TYPE"
        assert definition.minimum is not None and definition.maximum is not None
        if not definition.minimum <= value <= definition.maximum:
            return "OUT_OF_RANGE"
        return None
    if not isinstance(value, str):
        return "VALUE_TYPE"
    assert definition.max_chars is not None
    return "TOO_LONG" if len(value) > definition.max_chars else None


def assess(
    definition: PropertyDefinition,
    bound: DevelopmentBound | None,
    before: float | str,
    after: float | str,
    new_episodes: int,
    window_spent: float,
) -> Assessment:
    """Route and validity of one change. Operator route is never a way around the range check."""
    errors: list[str] = []
    if (problem := value_error(definition, after)) is not None:
        errors.append(problem)
    if before == after:
        errors.append("NO_CHANGE")
    if errors:
        return Assessment("operator", (), tuple(errors))
    reasons: list[str] = []
    if definition.layer == "identity":
        reasons.append("IDENTITY_NEEDS_OPERATOR")
    if bound is None or not bound.autonomous:
        reasons.append("NOT_AUTONOMOUS")
    if definition.kind == "text":
        reasons.append("TEXT_NEEDS_OPERATOR")
    if reasons:
        return Assessment("operator", tuple(reasons), ())
    assert bound is not None and not isinstance(before, str) and not isinstance(after, str)
    step = abs(after - before)
    per_episode = (bound.max_step_per_episode or 0.0) * max(new_episodes, 0)
    if new_episodes < bound.min_independent_episodes or new_episodes < 1:
        errors.append("NOT_ENOUGH_INDEPENDENT_EVIDENCE")
    if step > per_episode + NUMERIC_EPS:
        errors.append("STEP_TOO_LARGE")
    if bound.max_step_per_window is not None and (
        window_spent + step > bound.max_step_per_window + NUMERIC_EPS
    ):
        errors.append("WINDOW_BUDGET_EXCEEDED")
    return Assessment("autonomous", (), tuple(errors))
