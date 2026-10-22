"""Closed runtime payloads; clients never supply grants, model bindings or paths."""

from typing import Annotated, Literal, Self

from ai_agent_contracts.common import UTC, Contract, Id, Scope, Version
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.panel import ConfigureGM as ConfigureGM
from ai_agent_contracts.panel import RuleData as RuleData
from ai_agent_contracts.panel import RulesData as RulesData
from ai_agent_contracts.panel import register_panel_extensions
from pydantic import Field, model_validator


class Empty(Contract):
    pass


class TaskInput(Contract):
    kind: Literal[
        "extract",
        "consolidate",
        "reflect",
        "post_session",
        "gm",
        "laboratory",
        "dialogue",
        "activity",
    ]
    trigger_id: Id
    source_ids: list[Id] = Field(default_factory=list, max_length=200)
    episode_ids: list[Id] = Field(default_factory=list, max_length=20)
    arc_id: Id | None = None
    direction: Annotated[str, Field(max_length=2000)] = ""
    boundaries: Annotated[str, Field(max_length=2000)] = ""
    period_start: UTC | None = None
    period_end: UTC | None = None
    scenario_id: Id | None = None
    lab_scope: Scope | None = None
    seed: Annotated[str, Field(min_length=1, max_length=128)] = "lab-seed"
    # A resumed memory cycle starts at the first stage without a saved, proven outcome and
    # carries the records that earlier stages already produced; nothing is paid twice.
    resume_stage: Literal["extract", "consolidate", "reflect"] = "extract"
    resume_ids: list[Id] = Field(default_factory=list, max_length=100)
    resumed: bool = Field(default=False, strict=True)  # new generations for unsaved stages
    # A consolidate job over stored summaries instead of raw sources (knowledge digest).
    digest_of: list[Id] = Field(default_factory=list, max_length=31)
    work_session_id: Id | None = None
    work_session_generation: int | None = Field(default=None, strict=True, ge=1)
    work_session_attempt: int | None = Field(default=None, strict=True, ge=1, le=1000)
    work_session_sequence: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def inputs(self) -> Self:
        if (
            (self.work_session_id is None) != (self.work_session_generation is None)
            or (self.work_session_id is not None and self.kind != "activity")
            or (self.work_session_id is None) != (self.work_session_attempt is None)
            or (self.work_session_id is None and self.work_session_sequence is not None)
        ):
            raise ValueError("WORK_SESSION_INPUT")
        if len(self.source_ids) > 20 and not (self.kind == "consolidate" and self.digest_of):
            raise ValueError("TOO_MANY_SOURCES")
        if self.kind == "dialogue" and len(self.source_ids) != 1:
            raise ValueError("MESSAGE_REQUIRED")
        if self.kind in ("extract", "consolidate", "post_session", "gm") and not self.source_ids:
            raise ValueError("SOURCES_REQUIRED")
        if self.kind in ("reflect", "post_session") and not self.episode_ids:
            raise ValueError("EPISODES_REQUIRED")
        if self.kind in ("consolidate", "post_session") and (
            self.period_start is None
            or self.period_end is None
            or self.period_end < self.period_start
        ):
            raise ValueError("PERIOD_REQUIRED")
        if self.kind == "laboratory" and (
            self.scenario_id is None
            or self.lab_scope is None
            or self.lab_scope.memory_mode != "test"
            or self.lab_scope.agent_id is not None
        ):
            raise ValueError("SCENARIO_REQUIRED")
        return self


class JobArguments(Contract):
    request: TaskInput
    roles: dict[Id, Version] = Field(max_length=8)
    connections: dict[Id, Version] = Field(max_length=8)
    rules_version: Version
    gm_intensity: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    simulation_start: UTC


class TaskResult(Contract):
    status: Literal["completed", "cancelled", "failed"]
    record_ids: list[Id] = Field(default_factory=list, max_length=100)


class PermitLimits(Contract):
    max_calls: Annotated[int, Field(strict=True, ge=1, le=8)] = 8
    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=2048)] = 1024


class StoryVerdict(Contract):
    hard: list[Id] = Field(default_factory=list, max_length=200)
    soft: list[Id] = Field(default_factory=list, max_length=200)
    uncertain: bool = Field(default=True, strict=True)


def register(registry: ExtensionRegistry) -> None:
    register_panel_extensions(registry)
    for name, model in (
        ("empty", Empty),
        ("rules", RulesData),
        ("task", TaskInput),
        ("job", JobArguments),
        ("result", TaskResult),
        ("limits", PermitLimits),
        ("story-verdict", StoryVerdict),
    ):
        registry.register("m3." + name, model)
