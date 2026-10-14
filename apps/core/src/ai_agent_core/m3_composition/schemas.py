"""Closed runtime payloads; clients never supply grants, model bindings or paths."""

from typing import Annotated, Literal, Self

from ai_agent_contracts.common import UTC, Contract, Digest, Id, Scope, Version
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
        "extract", "consolidate", "reflect", "post_session", "gm", "laboratory", "dialogue"
    ]
    trigger_id: Id
    source_ids: list[Id] = Field(default_factory=list, max_length=20)
    episode_ids: list[Id] = Field(default_factory=list, max_length=20)
    arc_id: Id | None = None
    direction: Annotated[str, Field(max_length=2000)] = ""
    boundaries: Annotated[str, Field(max_length=2000)] = ""
    period_start: UTC | None = None
    period_end: UTC | None = None
    scenario_id: Id | None = None
    lab_scope: Scope | None = None
    seed: Annotated[str, Field(min_length=1, max_length=128)] = "lab-seed"

    @model_validator(mode="after")
    def inputs(self) -> Self:
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


class TransferInput(Contract):
    source_scope: Scope
    action: Literal["apply", "acknowledge"]
    operation_id: Id
    keys: list[Literal["profile", "gm_intensity", "story_rules_version"]] = Field(
        min_length=1, max_length=3
    )
    preview_digest: Digest | None = None


def register(registry: ExtensionRegistry) -> None:
    register_panel_extensions(registry)
    for name, model in (
        ("transfer", TransferInput),
        ("empty", Empty),
        ("rules", RulesData),
        ("task", TaskInput),
        ("job", JobArguments),
        ("result", TaskResult),
        ("limits", PermitLimits),
        ("story-verdict", StoryVerdict),
    ):
        registry.register("m3." + name, model)
