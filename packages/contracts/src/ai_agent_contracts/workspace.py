"""Workspace, capability and recovery metadata; never live bindings or provider payloads."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    ArtifactRef,
    Counter,
    Digest,
    ExtensionPayload,
    Id,
    Record,
    Scope,
    ShortText,
    SourceRef,
    Version,
    VersionedRecord,
)


class Workspace(VersionedRecord):
    settings: ExtensionPayload
    policy_version: Version
    active_agent_id: Id | None
    profile_ids: list[Id] = Field(max_length=200)
    connection_ids: list[Id] = Field(max_length=200)


class Capability(VersionedRecord):
    adapter_id: Id
    adapter_version: Id
    input_schema_id: Id
    output_schema_id: Id
    effect: Literal["none", "local_write", "external_effect", "public_output"]
    required_permissions: list[Id] = Field(max_length=200)
    resource_ids: list[Id] = Field(max_length=200)
    timeout_ms: int = Field(strict=True, ge=1, le=180000)
    cancellation_supported: bool = Field(strict=True)
    idempotency: Literal["supported", "unsupported", "unknown"]
    health: Literal["ready", "degraded", "unavailable"]
    setting_references: list[
        Annotated[str, Field(pattern=r"^AI_PLATFORM_[A-Z0-9_]+$", max_length=128)]
    ] = Field(max_length=200)


class Artifact(VersionedRecord):
    reference: ArtifactRef
    provenance_source: SourceRef
    expires_at: UTC | None
    protected: bool = Field(strict=True)

    @model_validator(mode="after")
    def same_artifact(self) -> Self:
        if self.reference.artifact_id != self.id or self.reference.version != self.version:
            raise ValueError("ARTIFACT_REFERENCE_MISMATCH")
        return self


class OutboxEntry(Record):
    owner_id: Id
    operation_id: Id
    resource_id: Id
    resource_version: Version
    event_id: Id
    causal_id: Id | None
    status: Literal["pending", "delivered", "failed"]
    delivery_count: Counter
    next_attempt_at: UTC | None


class ProcessedOperation(Record):
    owner_id: Id
    request_id: Id
    command_type: Id
    fingerprint: Digest
    receipt_id: Id
    expires_at: UTC


class DeletionTombstone(Record):
    source_id: Id
    source_version: Version
    blocked_at: UTC
    purged_at: UTC | None
    reason: Literal["operator_delete", "expired", "rights_revoked"]
    # No deleted content. A backup does not restore access based on its own age.


class LaboratoryReport(VersionedRecord):
    scenario_id: Id
    scenario_version: Version
    operation_id: Id
    status: Literal["pending", "running", "passed", "failed", "cancelled", "unknown"]
    report_artifact: ArtifactRef | None
    summary: ShortText

    @model_validator(mode="after")
    def test_only(self) -> Self:
        if self.scope.memory_mode != "test":
            raise ValueError("TEST_SCOPE_REQUIRED")
        return self


class LaboratoryScopeReservation(VersionedRecord):
    request_id: Id
    lab_scope: Scope

    @model_validator(mode="after")
    def isolated_area(self) -> Self:
        if (
            self.lab_scope.memory_mode != "test"
            or self.lab_scope.agent_id is not None
            or self.lab_scope.workspace_id != self.scope.workspace_id
            or self.lab_scope.persona_id != self.scope.persona_id
        ):
            raise ValueError("ISOLATED_LAB_SCOPE_REQUIRED")
        return self
