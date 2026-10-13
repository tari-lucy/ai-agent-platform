"""Canonical access bootstrap and authenticated service responses (no env bindings)."""

from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator

from ai_agent_contracts.common import UTC, Contract, Id, Scope
from ai_agent_contracts.control import ExecutionCommand, Lease


class OperatorLogin(Contract):
    password: SecretStr = Field(repr=False, json_schema_extra={"writeOnly": True})

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 1 <= len(raw.encode("utf-8")) <= 1024:
            raise ValueError("PASSWORD_LENGTH")
        return value


class OperatorSession(Contract):
    principal_id: Id
    scopes: list[Scope] = Field(min_length=1, max_length=8)
    expires_at: UTC
    # This anti-CSRF nonce is deliberately returned to the authenticated browser.
    # Password/hash/service token and opaque session cookie never enter this DTO.
    csrf_token: str = Field(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)


class ServiceClaimReceipt(Contract):
    request_id: Id
    scope: Scope
    command: ExecutionCommand | None
    lease: Lease | None
    acquired: bool = Field(strict=True)

    @model_validator(mode="after")
    def exact_lease(self) -> Self:
        if (self.command is None) != (self.lease is None):
            raise ValueError("CLAIM_PAIR")
        if self.command is None:
            if self.acquired:
                raise ValueError("CLAIM_EMPTY")
        elif self.lease is not None and (
            self.command.id != self.lease.command_id
            or self.command.scope != self.lease.scope
            or self.command.freshness != self.lease.freshness
        ):
            raise ValueError("CLAIM_LEASE_CONTEXT")
        return self


class ServiceLeaseReceipt(Contract):
    request_id: Id
    scope: Scope
    lease: Lease
