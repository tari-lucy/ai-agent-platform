"""Authoritative source references remain separate from derived recall."""

from typing import Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    ArtifactRef,
    Confidence,
    Contract,
    ExtensionPayload,
    Id,
    Record,
    Scope,
    ShortText,
    SourceRef,
    Text,
    TrueFlag,
    Version,
    VersionedRecord,
)
from ai_agent_contracts.states import ErrorCode, Provenance


class Retention(Contract):
    policy_id: Id
    expires_at: UTC | None
    protected: bool = Field(strict=True, default=False)


class Source(VersionedRecord):
    source_id: Id
    provenance: Provenance
    source_type: Id
    adapter_id: Id
    adapter_version: Id
    external_id: ShortText | None = None
    occurred_at: UTC
    registered_at: UTC
    author_id: Id | None = None
    identity_confidence: Confidence | None = None
    content: Text | None = None
    artifact: ArtifactRef | None = None
    retention: Retention
    availability: Literal["available", "expired", "deleted", "unavailable"]
    read_allowed: bool = Field(strict=True)
    api_transfer_allowed: bool = Field(strict=True)
    public_disclosure_allowed: bool = Field(strict=True)

    @model_validator(mode="after")
    def no_removed_content(self) -> Self:
        if self.source_id != self.id:
            raise ValueError("SOURCE_ID_MISMATCH")
        if self.availability != "available" and (
            self.content is not None or self.artifact is not None
        ):
            raise ValueError("SOURCE_UNAVAILABLE")
        return self


class Episode(Record):
    sources: list[SourceRef] = Field(min_length=1, max_length=200)
    participant_ids: list[Id] = Field(max_length=200)
    choice: Text
    actual_outcome: Text
    interpretation: Text | None = None
    operation_id: Id
    provenance: Literal["observation", "simulation"]
    outcome_confirmed: TrueFlag


class MemoryCorrection(Contract):
    """Operator annotation; inherits the record's source rights and retention dependencies."""

    operator_id: Id
    request_id: Id
    previous_version: Version
    reason: str = Field(strict=True, min_length=1, max_length=1000)


class Claim(VersionedRecord):
    subject_id: Id
    content: Text
    knowledge_type: Literal["observed", "reported", "canon", "hypothesis", "interpretation"]
    sources: list[SourceRef] = Field(min_length=1, max_length=200)
    confidence: Confidence
    conflict_ids: list[Id] = Field(default_factory=list, max_length=200)
    correction: MemoryCorrection | None = None


class Summary(VersionedRecord):
    period_start: UTC
    period_end: UTC
    episode_ids: list[Id] = Field(max_length=200)
    sources: list[SourceRef] = Field(min_length=1, max_length=200)
    content: Text
    analysis_id: Id
    correction: MemoryCorrection | None = None


class AccountLink(Contract):
    platform_id: Id
    account_id: ShortText
    display_name: ShortText
    confirmation: Literal["confirmed", "hypothesis", "rejected"]
    evidence: list[SourceRef] = Field(min_length=1, max_length=200)


class Person(VersionedRecord):
    person_id: Id
    accounts: list[AccountLink] = Field(max_length=200)


class Encounter(Record):
    person_ids: list[Id] = Field(min_length=1, max_length=200)
    sources: list[SourceRef] = Field(min_length=1, max_length=200)


class Relationship(VersionedRecord):
    person_id: Id
    # The character defines aspect names and bounds, never a fixed emotion enum.
    aspects: ExtensionPayload
    change_ids: list[Id] = Field(default_factory=list, max_length=200)
    grounds: list[SourceRef] = Field(max_length=200)


class Topic(VersionedRecord):
    topic_id: Id
    participant_ids: list[Id] = Field(min_length=1, max_length=200)
    utterance_ids: list[Id] = Field(default_factory=list, max_length=200)
    sources: list[SourceRef] = Field(max_length=200)
    status: Literal["open", "reserved", "awaiting_answer", "answered", "closed"]
    answer: Text | None = None
    reserved_by_operation_id: Id | None = None
    reservation_deadline: UTC | None = None
    cooldown_until: UTC | None = None

    @model_validator(mode="after")
    def finite_reservation(self) -> Self:
        if self.status == "reserved" and (
            self.reserved_by_operation_id is None or self.reservation_deadline is None
        ):
            raise ValueError("RESERVATION_REQUIRED")
        return self


class Promise(VersionedRecord):
    participant_ids: list[Id] = Field(min_length=1, max_length=200)
    topic_id: Id
    delivered_utterance_id: Id
    delivery_id: Id
    sources: list[SourceRef] = Field(min_length=1, max_length=200)
    content: Text
    deadline: UTC | None = None
    completion_criterion: Text
    status: Literal["open", "fulfilled", "withdrawn", "unknown"]
    closure_result: Text | None = None

    @model_validator(mode="after")
    def closure_required(self) -> Self:
        if self.status in {"fulfilled", "withdrawn"} and self.closure_result is None:
            raise ValueError("CLOSURE_REQUIRED")
        return self


class RecallRequest(Contract):
    scope: Scope
    query: Text = Field(min_length=1)
    limit: int = Field(strict=True, ge=1, le=200, default=8)
    context_token_limit: int = Field(strict=True, ge=1, le=2000, default=2000)
    purpose: Literal["private_context", "api_transfer", "public_disclosure"]


class RecallFragment(Contract):
    source: SourceRef
    content: Text
    attribution: ShortText
    conflicts: list[Id] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def source_available(self) -> Self:
        if self.source.availability != "available":
            raise ValueError("SOURCE_UNAVAILABLE")
        return self


class RecallResult(Contract):
    scope: Scope
    fragments: list[RecallFragment] = Field(max_length=200)
    complete: bool = Field(strict=True)
    index_state: Literal["ready", "lagging", "degraded", "unavailable"]
    missing_source_ids: list[Id] = Field(default_factory=list, max_length=200)


class MemoryCycle(VersionedRecord):
    """Bounded processing receipt. IDs/status only; it does not retain raw or derived text."""

    episode_ids: list[Id] = Field(min_length=1, max_length=10)
    source_ids: list[Id] = Field(min_length=1, max_length=20)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_id: Id | None = None
    stage: Literal["queued", "extract", "consolidate", "reflect", "done"]
    status: Literal["queued", "running", "completed", "failed", "unknown"]
    record_ids: list[Id] = Field(default_factory=list, max_length=100)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def progress(self) -> Self:
        if (
            (self.status == "completed") != (self.stage == "done")
            or self.status == "queued"
            and self.stage != "queued"
            or self.status == "running"
            and self.stage in ("queued", "done")
            or (self.status in ("failed", "unknown")) != (self.error_code is not None)
        ):
            raise ValueError("MEMORY_CYCLE_PROGRESS")
        return self
