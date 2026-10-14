"""Run, job, lease and result contracts. DTO validation never grants execution."""

import hashlib
from typing import Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    ArtifactRef,
    Contract,
    Counter,
    Digest,
    ExtensionPayload,
    Id,
    Record,
    Scope,
    ShortText,
    SourceRef,
    Text,
    Version,
    VersionedRecord,
)
from ai_agent_contracts.states import (
    ActivityState,
    DeliveryState,
    OperationState,
    Provenance,
    RunState,
)


class Freshness(Contract):
    run_id: Id
    control_epoch: Counter
    profile_version: Version
    policy_version: Version


class RunSession(VersionedRecord):
    agent_id: Id
    run_id: Id
    session_id: Id
    state: RunState
    autonomy: Literal["observation", "autonomous"]
    activity: ActivityState
    voice_enabled: bool = Field(strict=True)
    gm_paused: bool = Field(strict=True)
    control_epoch: Counter
    started_at: UTC | None = None
    ended_at: UTC | None = None
    unconfirmed_resource_ids: list[Id] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (self.agent_id, self.run_id, self.session_id) != (
            self.scope.agent_id,
            self.scope.run_id,
            self.scope.session_id,
        ):
            raise ValueError("SCOPE_MISMATCH")
        return self


class MessageEnvelope(Record):
    type: Id
    adapter_id: Id
    adapter_version: Id
    occurred_at: UTC
    registered_at: UTC
    source_sequence: Counter | None = None
    provenance: Provenance
    trace_id: Id
    causal_id: Id | None = None
    episode_id: Id | None = None
    utterance_id: Id | None = None
    operation_id: Id | None = None
    sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=200)
    payload: ExtensionPayload


class ActionProposal(Record):
    type: Literal["action.proposal"]
    goal: Text
    grounds: list[SourceRef] = Field(min_length=1, max_length=200)
    expected_outcome: Text
    applicability: Text
    priority: int = Field(strict=True, ge=0, le=100)
    expires_at: UTC
    capability_ids: list[Id] = Field(min_length=1, max_length=200)


class ExecutionCommand(Record):
    type: Literal["execution.command"]
    operation_id: Id
    target: Id
    binding_id: Id
    capability_id: Id
    capability_version: Version
    arguments: ExtensionPayload
    permission_id: Id
    permission_version: Version
    deadline: UTC
    conditions: ExtensionPayload
    cancellation_id: Id
    freshness: Freshness
    sources: list[SourceRef] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def current_run(self) -> Self:
        if self.scope.run_id is None or self.scope.run_id != self.freshness.run_id:
            raise ValueError("RUN_MISMATCH")
        return self


class Lease(Contract):
    operation_id: Id
    command_id: Id
    attempt_id: Id
    lease_version: Version
    worker_id: Id
    scope: Scope
    freshness: Freshness
    expires_at: UTC

    @model_validator(mode="after")
    def current_run(self) -> Self:
        if self.scope.run_id is None or self.scope.run_id != self.freshness.run_id:
            raise ValueError("RUN_MISMATCH")
        return self


class Job(VersionedRecord):
    operation_id: Id
    command_id: Id
    goal: Text
    capability_id: Id
    capability_version: Version
    status: OperationState
    deadline: UTC
    conditions: ExtensionPayload
    freshness: Freshness
    waiting_reason: ShortText | None = None
    wake_event: Id | None = None
    lease: Lease | None = None

    @model_validator(mode="after")
    def job_state(self) -> Self:
        if self.scope.run_id != self.freshness.run_id:
            raise ValueError("RUN_MISMATCH")
        if self.status == OperationState.WAITING and (
            self.waiting_reason is None or self.wake_event is None
        ):
            raise ValueError("WAIT_CONDITION_REQUIRED")
        if self.status == OperationState.RUNNING and self.lease is None:
            raise ValueError("LEASE_REQUIRED")
        if self.lease is not None and (
            self.lease.scope != self.scope
            or self.lease.freshness != self.freshness
            or self.lease.operation_id != self.operation_id
            or self.lease.command_id != self.command_id
        ):
            raise ValueError("LEASE_CONTEXT_MISMATCH")
        return self


class ExecutionResult(Record):
    type: Literal["execution.result"]
    command_id: Id
    operation_id: Id
    attempt_id: Id
    lease_version: Version
    freshness: Freshness
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED", "OUTCOME_UNKNOWN"]
    evidence: list[SourceRef] = Field(max_length=200)
    completed_at: UTC
    external_ids: list[ShortText] = Field(default_factory=list, max_length=200)
    result: ExtensionPayload | None = None

    @model_validator(mode="after")
    def run_matches(self) -> Self:
        if self.scope.run_id != self.freshness.run_id:
            raise ValueError("RUN_MISMATCH")
        return self


class Utterance(VersionedRecord):
    utterance_id: Id
    revision: Version
    content: Text
    content_hash: Digest
    audience_id: Id
    channel_ids: list[Id] = Field(min_length=1, max_length=200)
    moderation_status: Literal["draft", "checking", "approved", "blocked"]
    moderation_check_id: Id | None = None
    # Private replies retain their grounds for an explicit panel receipt, including restart.
    sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    operation_id: Id | None = None

    @model_validator(mode="after")
    def approval_requires_check(self) -> Self:
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("CONTENT_HASH_MISMATCH")
        if self.moderation_status == "approved" and self.moderation_check_id is None:
            raise ValueError("CHECK_REQUIRED")
        return self


class ModerationCheck(Record):
    utterance_id: Id
    revision: Version
    content_hash: Digest
    audience_id: Id
    policy_version: Version
    context_hash: Digest
    freshness: Freshness
    expires_at: UTC
    subject_model_id: Id
    moderator_model_id: Id
    decision: Literal["approved", "blocked", "unavailable"]
    reason_code: Id

    @model_validator(mode="after")
    def independent_model(self) -> Self:
        if self.subject_model_id == self.moderator_model_id:
            raise ValueError("INDEPENDENT_MODEL_REQUIRED")
        if self.scope.run_id != self.freshness.run_id:
            raise ValueError("RUN_MISMATCH")
        return self


class Delivery(VersionedRecord):
    utterance_id: Id
    revision: Version
    channel_id: Id
    attempt_id: Id
    status: DeliveryState
    started_at: UTC | None = None
    completed_at: UTC | None = None
    interrupted_at: UTC | None = None
    external_id: ShortText | None = None


class OperationProjection(VersionedRecord):
    operation_id: Id
    status: OperationState
    deadline: UTC
    waiting_reason: ShortText | None = None
    wake_event: Id | None = None
    result_id: Id | None = None

    @model_validator(mode="after")
    def finite_wait(self) -> Self:
        if self.status == OperationState.WAITING and (
            self.waiting_reason is None or self.wake_event is None
        ):
            raise ValueError("WAIT_CONDITION_REQUIRED")
        if self.status == OperationState.SUCCEEDED and self.result_id is None:
            raise ValueError("RESULT_REQUIRED")
        return self
