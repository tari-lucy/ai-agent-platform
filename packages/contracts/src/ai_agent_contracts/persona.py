"""Profile, experience and development DTOs; owners implement persistence later."""

import math
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    Confidence,
    Contract,
    ExtensionPayload,
    Id,
    ShortText,
    SourceRef,
    Text,
    Version,
    VersionedRecord,
)
from ai_agent_contracts.states import ChangeState, OpportunityState


class Profile(VersionedRecord):
    persona_id: Id
    canon: Text
    style: Text
    property_definitions: ExtensionPayload
    initial_values: ExtensionPayload
    development_bounds: ExtensionPayload
    accepted_revision_id: Id

    @model_validator(mode="after")
    def persona_matches_scope(self) -> Self:
        if self.persona_id != self.scope.persona_id:
            raise ValueError("SCOPE_MISMATCH")
        return self


class CharacterState(VersionedRecord):
    persona_id: Id
    profile_version: Version
    state_version: Version
    values: ExtensionPayload
    last_change_ids: list[Id] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def versions_match(self) -> Self:
        if self.persona_id != self.scope.persona_id:
            raise ValueError("SCOPE_MISMATCH")
        if self.state_version != self.version:
            raise ValueError("VERSION_MISMATCH")
        return self


class Briefing(VersionedRecord):
    author_id: Id
    type: Literal["context", "canon_proposal", "scenario"]
    content: Text
    expires_at: UTC
    status: Literal["active", "cancelled", "expired"]


class StoryArc(VersionedRecord):
    grounds: list[SourceRef] = Field(min_length=1, max_length=200)
    episode_ids: list[Id] = Field(default_factory=list, max_length=200)
    direction: Text
    boundaries: Text
    status: Literal["open", "paused", "closed"]
    cooldown_until: UTC | None = None
    rules_version: Version | None = None


class StoryRoll(Contract):
    seed: ShortText
    context: ShortText
    options: list[ShortText] = Field(max_length=256)
    weights: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(max_length=256)
    chosen: ShortText
    value: Annotated[float, Field(allow_inf_nan=False)]

    @model_validator(mode="after")
    def consistent_choice(self) -> Self:
        if len(self.options) != len(self.weights):
            raise ValueError("ROLL_WEIGHTS")
        if self.options and (
            self.chosen not in self.options or not 0 < sum(self.weights) < math.inf
        ):
            raise ValueError("ROLL_CHOICE")
        return self


class Opportunity(VersionedRecord):
    arc_id: Id
    grounds: list[SourceRef] = Field(min_length=1, max_length=200)
    applicability: Text
    boundaries: Text
    expires_at: UTC
    capability_ids: list[Id] = Field(max_length=200)
    status: OpportunityState
    choice: Text | None = None
    actual_outcome: Text | None = None
    family_id: Id | None = None
    topic_key: ShortText | None = None
    reason_code: ShortText | None = None
    rules_version: Version | None = None
    rolls: list[StoryRoll] = Field(default_factory=list, max_length=3)


class StateChange(VersionedRecord):
    proposal_id: Id
    owner: Id
    base_version: Version
    patch: ExtensionPayload
    evidence: list[SourceRef] = Field(min_length=1, max_length=200)
    interpretation: Text
    uncertainty: Confidence
    expected_manifestation: Text
    checks: list[Id] = Field(default_factory=list, max_length=200)
    status: ChangeState
    operator_decision_id: Id | None = None
    new_version: Version | None = None
    revision_of: Id | None = None

    @model_validator(mode="after")
    def applied_version(self) -> Self:
        if self.status == ChangeState.APPLIED and (
            self.new_version is None or self.new_version <= self.base_version
        ):
            raise ValueError("NEW_VERSION_REQUIRED")
        if self.status != ChangeState.APPLIED and self.new_version is not None:
            raise ValueError("NOT_APPLIED")
        return self
