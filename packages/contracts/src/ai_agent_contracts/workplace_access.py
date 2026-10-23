"""One operator decision for the configured initial workplace, not a wildcard permission."""

from pydantic import Field

from .agent_workplace import WorkplaceArea
from .common import UTC, Contract, Id, Scope, Text, Version, VersionedRecord


class InitialAccessArguments(Contract):
    duration_seconds: int = Field(strict=True, ge=60, le=31_536_000)
    constraints: Text = ""


class InitialAccessMember(Contract):
    capability_id: Id
    manifest_id: Id
    manifest_version: Version
    binding_id: Id
    binding_version: Version
    permission_id: Id
    permission_version: Version | None
    areas: list[WorkplaceArea] = Field(max_length=8)


class InitialAccessProof(VersionedRecord):
    operator_id: Id
    capabilities: list[Id] = Field(min_length=1, max_length=8)
    members: list[InitialAccessMember] = Field(min_length=1, max_length=8)
    decision: InitialAccessArguments
    policy_version: Version
    plan_expires_at: UTC
    expires_at: UTC
    executed_by: Id | None = None


class InitialAccessCapability(Contract):
    capability_id: Id
    summary: Text
    areas: list[WorkplaceArea] = Field(max_length=8)
    available: bool
    expires_at: UTC | None


class InitialAccessStatus(Contract):
    scope: Scope
    configured: bool
    ready: bool
    max_duration_seconds: int = Field(strict=True, ge=0, le=31_536_000)
    members: list[InitialAccessCapability] = Field(max_length=8)
    unavailable_reason: Id | None = None
