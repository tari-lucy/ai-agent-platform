"""Canonical owner payloads and projections for the private panel."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .common import UTC, Contract, Digest, Id, Scope, ShortText, Version, VersionedRecord
from .extensions import ExtensionRegistry
from .persona import SettingsTransferOperation

MarkdownText = Annotated[str, Field(strict=True, min_length=1, max_length=30000)]


class PersonaCreateArguments(Contract):
    markdown: MarkdownText


class PersonaValidateArguments(Contract):
    markdown: MarkdownText


class PersonaApplyArguments(Contract):
    markdown: MarkdownText
    revision_digest: Digest


class PersonaCopyArguments(Contract):
    source_scope: Scope
    source_version: Version


class RuleData(Contract):
    rule_id: Id
    level: Literal["project", "profile", "session"]
    kind: Literal["hard", "soft", "desired"]
    description: Annotated[str, Field(min_length=1, max_length=1500)]
    markers: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=30
    )


class RulesData(Contract):
    rules: list[RuleData] = Field(max_length=200)


class ConfigureGM(Contract):
    intensity: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    rules: RulesData | None = None
    rules_version: Version | None = None
    paused: bool | None = None


class LaboratoryCreateArguments(Contract):
    persona_id: Id


class LaboratoryStartArguments(Contract):
    scenario_id: Id
    scenario_version: Version
    lab_scope: Scope
    seed: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    mode: Literal["mock"] = "mock"

    @model_validator(mode="after")
    def isolated(self) -> Self:
        if self.lab_scope.memory_mode != "test" or self.lab_scope.agent_id is not None:
            raise ValueError("FRESH_TEST_SCOPE_REQUIRED")
        return self


class LaboratoryScenario(Contract):
    scenario_id: Id
    version: Version
    title: ShortText
    tags: list[ShortText] = Field(max_length=30)


class LaboratoryCatalogue(Contract):
    scope: Scope
    scenarios: list[LaboratoryScenario] = Field(max_length=100)
    mode: Literal["mock"] = "mock"


class ProfileMarkdown(Contract):
    scope: Scope
    profile_id: Id
    version: Version
    markdown: MarkdownText


class ProfileIssue(Contract):
    code: ShortText
    message: ShortText
    line: Annotated[int, Field(strict=True, ge=1)] | None = None
    section: ShortText | None = None


class ProfileDiffProjection(Contract):
    canon_changed: bool
    style_changed: bool
    properties_added: list[Id] = Field(max_length=100)
    properties_removed: list[Id] = Field(max_length=100)
    properties_changed: list[Id] = Field(max_length=100)
    initial_values_changed: list[Id] = Field(max_length=100)
    bounds_changed: list[Id] = Field(max_length=100)


class ProfileValidation(Contract):
    request_id: Id
    scope: Scope
    issues: list[ProfileIssue] = Field(max_length=200)
    blockers: list[ProfileIssue] = Field(max_length=200)
    digest: Digest | None
    current_version: Version | None
    version_conflict: bool
    unchanged: bool
    ready: bool
    diff: ProfileDiffProjection | None


def register_panel_extensions(registry: ExtensionRegistry) -> None:
    registry.register("gm.configure-args", ConfigureGM)
    registry.register("laboratory.start-args", LaboratoryStartArguments)
    registry.register("laboratory.create-args", LaboratoryCreateArguments)
    registry.register("laboratory.transfer-settings-args", SettingsTransferArguments)


SettingsTransferKey = Literal["profile", "gm_intensity", "story_rules_version"]


class SettingsTransferArguments(Contract):
    source_scope: Scope
    action: Literal["preview", "apply", "acknowledge"]
    operation_id: Id
    keys: list[SettingsTransferKey] = Field(min_length=1, max_length=3)
    preview_digest: Digest | None = None
    plan_id: Id | None = None
    plan_version: Version | None = None

    @model_validator(mode="after")
    def binding(self) -> Self:
        if len(set(self.keys)) != len(self.keys) or self.source_scope.memory_mode != "test":
            raise ValueError("TRANSFER_SELECTION")
        if self.action != "preview" and self.preview_digest is None:
            raise ValueError("TRANSFER_DIGEST_REQUIRED")
        if self.action == "apply" and (self.plan_id is None or self.plan_version is None):
            raise ValueError("TRANSFER_PLAN_REQUIRED")
        return self


class SettingsTransferItem(Contract):
    key: SettingsTransferKey
    before: JsonValue
    after: JsonValue
    changes: list[ShortText] = Field(max_length=200)
    blockers: list[ShortText] = Field(max_length=200)


class SettingsTransferPreview(VersionedRecord):
    request_id: Id
    operation_id: Id
    operator_id: Id
    source_scope: Scope
    keys: list[SettingsTransferKey] = Field(min_length=1, max_length=3)
    preview_digest: Digest
    target_version: Version
    expires_at: UTC
    items: list[SettingsTransferItem] = Field(min_length=1, max_length=3)
    never_transferred: list[ShortText] = Field(max_length=30)
    blocked: bool

    @model_validator(mode="after")
    def selection(self) -> Self:
        if (
            self.scope.memory_mode != "work"
            or self.source_scope.memory_mode != "test"
            or self.scope.workspace_id != self.source_scope.workspace_id
            or len(set(self.keys)) != len(self.keys)
            or [i.key for i in self.items] != self.keys
            or self.blocked != any(i.blockers for i in self.items)
            or self.expires_at <= self.created_at
        ):
            raise ValueError("TRANSFER_PREVIEW")
        return self


class SettingsTransferStatus(Contract):
    scope: Scope
    operation_id: Id
    record: SettingsTransferOperation | None
