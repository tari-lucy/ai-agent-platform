"""Safe LLM and cost projections; decimal strings and unknown usage stay explicit."""

from typing import Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    Contract,
    Counter,
    ExtensionPayload,
    Id,
    Money,
    ShortText,
    Version,
    VersionedRecord,
)


class Connection(VersionedRecord):
    name: ShortText
    adapter_id: Id
    adapter_version: Id
    # Setting references are env VARIABLE NAMES, never their values.
    endpoint_setting: str = Field(pattern=r"^AI_PLATFORM_[A-Z0-9_]+$", max_length=128)
    credential_setting: str = Field(pattern=r"^AI_PLATFORM_[A-Z0-9_]+$", max_length=128)
    configured: bool = Field(strict=True)
    verification: Literal["unverified", "verified", "failed"]
    capabilities: list[Id] = Field(max_length=200)


class LLMAssignment(VersionedRecord):
    role_id: Id
    connection_id: Id
    model_id: Id
    parameters: ExtensionPayload
    required_capabilities: list[Id] = Field(max_length=200)
    capability_verification: Literal["unverified", "verified", "failed"]
    fallback_assignment_id: Id | None = None


class TokenUsage(Contract):
    input_tokens: Counter | None
    output_tokens: Counter | None
    cached_input_tokens: Counter | None = None
    reasoning_output_tokens: Counter | None = None
    source: Literal["provider", "local_estimate", "unknown"]
    completeness: Literal["complete", "partial", "unknown"]

    @model_validator(mode="after")
    def categories_are_subsets(self) -> Self:
        for total, subset in (
            (self.input_tokens, self.cached_input_tokens),
            (self.output_tokens, self.reasoning_output_tokens),
        ):
            if total is not None and subset is not None and subset > total:
                raise ValueError("TOKEN_SUBSET_OVERFLOW")
        if self.completeness == "complete" and (
            self.input_tokens is None or self.output_tokens is None
        ):
            raise ValueError("USAGE_INCOMPLETE")
        if self.source == "unknown" and any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cached_input_tokens,
                self.reasoning_output_tokens,
            )
        ):
            raise ValueError("UNKNOWN_USAGE")
        return self


class LLMAttempt(VersionedRecord):
    operation_id: Id
    attempt_id: Id
    role_id: Id
    assignment_id: Id
    assignment_version: Version
    requested_model_id: Id
    reported_model_id: Id | None = None
    status: Literal["running", "completed", "failed", "cancelled", "incomplete", "unknown"]
    started_at: UTC
    completed_at: UTC | None = None
    usage: TokenUsage
    tariff_id: Id | None = None
    tariff_version: Version | None = None

    @model_validator(mode="after")
    def tariff_pair(self) -> Self:
        if (self.tariff_id is None) != (self.tariff_version is None):
            raise ValueError("TARIFF_REFERENCE_INCOMPLETE")
        return self


class Tariff(VersionedRecord):
    connection_id: Id
    model_id: Id
    unit_tokens: int = Field(strict=True, ge=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_rate: Money | None
    output_rate: Money | None
    cached_input_rate: Money | None = None
    reasoning_output_rate: Money | None = None
    effective_at: UTC


class CostCalculation(VersionedRecord):
    attempt_id: Id
    tariff_id: Id | None
    tariff_version: Version | None
    amount: Money | None
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    completeness: Literal["complete", "partial", "unknown"]
    provenance: Literal["provider_usage", "local_estimate", "unknown"]

    @model_validator(mode="after")
    def unknown_is_not_zero(self) -> Self:
        if (self.tariff_id is None) != (self.tariff_version is None):
            raise ValueError("TARIFF_REFERENCE_INCOMPLETE")
        if self.completeness == "complete" and (
            self.amount is None
            or self.currency is None
            or self.tariff_id is None
            or self.provenance == "unknown"
        ):
            raise ValueError("COST_INCOMPLETE")
        if self.completeness == "unknown" and self.amount is not None:
            raise ValueError("UNKNOWN_COST")
        return self
