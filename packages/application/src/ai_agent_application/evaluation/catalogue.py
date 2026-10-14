"""The twenty quality scenarios: twelve reference (Q-01..Q-12) plus eight new (Q-13..Q-20)."""

from .catalogue_new import new_scenarios
from .catalogue_reference import reference_scenarios
from .model import (
    Fact,
    Forget,
    Gap,
    Live,
    NewSession,
    ProviderFault,
    QualityScenario,
    Restart,
    Resume,
    Say,
    Stop,
)
from .rubric import CRITERION_IDS

EXPECTED_SCENARIOS = 20
_KNOWN_STEPS = (Say, Gap, NewSession, Live, Fact, Forget, Stop, Restart, Resume, ProviderFault)


class CatalogueError(ValueError):
    """The catalogue is malformed; the message names the scenario, never any text."""


def quality_catalogue() -> list[QualityScenario]:
    found = [*reference_scenarios(), *new_scenarios()]
    validate(found)
    return found


def validate(scenarios: list[QualityScenario]) -> None:
    if len(scenarios) != EXPECTED_SCENARIOS:
        raise CatalogueError(f"SCENARIO_COUNT={len(scenarios)}")
    ids = [s.scenario_id for s in scenarios]
    if len(set(ids)) != len(ids) or len({s.slug for s in scenarios}) != len(ids):
        raise CatalogueError("DUPLICATE_ID_OR_SLUG")
    for s in scenarios:
        if not s.steps or not s.messages:
            raise CatalogueError(f"{s.scenario_id}:NO_MESSAGES")
        if not set(s.focus) <= CRITERION_IDS:
            raise CatalogueError(f"{s.scenario_id}:UNKNOWN_CRITERION")
        if not s.expect.strip() or not s.failure.strip():
            raise CatalogueError(f"{s.scenario_id}:EXPECTATION_MISSING")
        keys: set[str] = set()
        for step in s.steps:
            if not isinstance(step, _KNOWN_STEPS):
                raise CatalogueError(f"{s.scenario_id}:UNKNOWN_STEP")
            if isinstance(step, Say) and step.who != "operator" and step.who not in s.people:
                raise CatalogueError(f"{s.scenario_id}:UNKNOWN_PERSON")
            if isinstance(step, Fact) and step.key:
                keys.add(step.key)
            if isinstance(step, Forget) and step.key not in keys:
                raise CatalogueError(f"{s.scenario_id}:FORGET_BEFORE_FACT")


__all__ = ["EXPECTED_SCENARIOS", "CatalogueError", "quality_catalogue", "validate"]
