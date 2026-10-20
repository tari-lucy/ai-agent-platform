"""Activity contour shapes (AW-05): the model's closed decision and the trusted settings.

The decision is data. It cannot grant a right, name a host path or start anything: the step
maps it to owner commands (task update, ticket, permitted tool call) after its own checks.
"""

from dataclasses import dataclass
from typing import Literal, Self

from ai_agent_contracts.common import Contract, Id
from pydantic import Field, model_validator

MAX_TEXT = 2000


class StepDecision(Contract):
    """One step of one task, chosen by the agent from the capabilities it was shown."""

    action: Literal["complete", "use_capability", "ask_operator", "defer", "abandon"]
    summary: str = Field(strict=True, min_length=1, max_length=MAX_TEXT)
    capability_id: Id | None = None
    arguments: dict[str, str] = Field(default_factory=dict, max_length=20)
    question: str | None = Field(default=None, strict=True, max_length=MAX_TEXT)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.action == "use_capability") != (self.capability_id is not None):
            raise ValueError("CAPABILITY_SHAPE")
        if self.action == "ask_operator" and not (self.question or "").strip():
            raise ValueError("QUESTION_REQUIRED")
        if any(len(key) > 64 or len(value) > MAX_TEXT for key, value in self.arguments.items()):
            raise ValueError("ARGUMENTS_TOO_LARGE")
        return self


class GoalProposal(Contract):
    """An own goal the agent chooses when nothing is assigned; ``none`` is a valid answer."""

    action: Literal["goal", "none"]
    goal: str | None = Field(default=None, strict=True, max_length=MAX_TEXT)
    reason: str = Field(strict=True, max_length=MAX_TEXT)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.action == "goal") != bool((self.goal or "").strip()):
            raise ValueError("GOAL_SHAPE")
        return self


@dataclass(frozen=True)
class ActivitySettings:
    """Trusted configuration; values are the operator's, not constants of this module."""

    cooldown_seconds: float = 600.0
    check_seconds: float = 30.0  # how often the tick looks for work; the look itself is free
    own_goals: bool = False
    own_goal_daily: int = 2
    max_open_tasks: int = 20
    max_attempts_per_task: int = 6
    manifest_chars: int = 6000

    def __post_init__(self) -> None:
        if (
            not 0 <= self.cooldown_seconds <= 86400
            or not 0 <= self.check_seconds <= 3600
            or not 0 <= self.own_goal_daily <= 24
            or not 1 <= self.max_open_tasks <= 200
            or not 1 <= self.max_attempts_per_task <= 100
            or not 500 <= self.manifest_chars <= 20000
        ):
            raise ValueError("ACTIVITY_SETTINGS")


ORIGIN_ORDER: dict[str, int] = {
    "assignment": 0,
    "ticket_followup": 1,
    "commitment": 2,
    "own_goal": 3,
}
