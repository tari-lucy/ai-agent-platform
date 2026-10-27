"""Trusted owner ports; authentication is composed by M1-05, never a broker message."""

import math
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import ExecutionCommand, ExecutionResult, Job, Lease
from ai_agent_contracts.states import ErrorCode


@dataclass(frozen=True)
class WorkerAuthority:
    id: str
    partitions: frozenset[tuple[str, str, str, str]]
    capability_ids: frozenset[str]
    expires_at: float


@dataclass(frozen=True)
class ExecutionPolicy:
    """Proof of current rights/sources/conditions/moderation, not adapter self-report.

    Retry-safe means duplicate execution cannot duplicate an external effect.
    A worker result cannot grant itself retry permission.
    """

    command_id: str
    scope: Scope
    retry_safe: bool
    expires_at: float


class Policy(Protocol):
    async def check(self, command: ExecutionCommand, worker_id: str | None) -> ExecutionPolicy: ...


@dataclass(frozen=True)
class Schedule:
    job_id: str
    operation_id: str
    scope: Scope
    fingerprint: str
    attempts: int
    ready_at: datetime
    retry_safe: bool


type AttemptStatus = Literal["running", "succeeded", "failed", "cancelled", "expired", "unknown"]


@dataclass(frozen=True)
class Attempt:
    lease: Lease
    status: AttemptStatus
    finished_at: datetime | None = None
    error: ErrorCode | None = None


@dataclass(frozen=True)
class JobPage:
    items: list[tuple[Job, Schedule]]
    next_cursor: str | None


@dataclass(frozen=True)
class DispatchRef:
    event_id: str
    operation_id: str
    resource_id: str
    resource_version: int
    scope: Scope


@dataclass(frozen=True)
class Delivery:
    id: str
    reference: DispatchRef


class Broker(Protocol):
    async def publish(self, reference: DispatchRef) -> str: ...
    async def read(self, consumer: str, *, count: int = 1) -> list[Delivery]: ...
    async def reclaim(self, consumer: str, *, idle_ms: int, count: int = 1) -> list[Delivery]: ...
    async def acknowledge(self, delivery_id: str) -> None: ...


class JobTransaction(Protocol):
    async def save(
        self,
        job: Job,
        schedule: Schedule,
        *,
        expected_version: int | None,
        attempt: Attempt | None = None,
        result: ExecutionResult | None = None,
        reconciliation: ExecutionResult | None = None,
    ) -> None: ...


class JobStore(Protocol):
    async def command(self, command_id: str, scope: Scope) -> ExecutionCommand: ...
    async def load(self, operation_id: str, scope: Scope) -> tuple[Job, Schedule] | None: ...
    async def active(self, *, limit: int, cursor: str | None = None) -> JobPage: ...
    async def receipt(self, command: CommandBase) -> Receipt | None: ...
    async def result(self, result_id: str, scope: Scope) -> ExecutionResult | None: ...
    async def attempt(self, operation_id: str, attempt_id: str, scope: Scope) -> Attempt | None: ...
    async def reconciliation(self, receipt_id: str, scope: Scope) -> str | None: ...
    def transaction(
        self,
        command: CommandBase,
        *,
        pending_limit: int,
    ) -> AbstractAsyncContextManager[JobTransaction]: ...
    async def pending_events(self, *, limit: int) -> list[DispatchRef]: ...
    async def dispatched(self, reference: DispatchRef, *, retry_at: datetime) -> None: ...
    async def delivery_failed(self, reference: DispatchRef, *, retry_at: datetime) -> None: ...


@dataclass(frozen=True)
class JobLimits:
    pending: int = 256
    batch: int = 50
    attempts: int = 2
    lease_seconds: float = 20
    # D76: a long work turn of the agent is one job; its own wall time bounds it first.
    total_seconds: float = 86400
    retry_seconds: float = 1
    dispatch_seconds: float = 5

    def __post_init__(self) -> None:
        if not (1 <= self.pending <= 256 and 1 <= self.batch <= 200 and 1 <= self.attempts <= 2):
            raise ValueError("JOB_LIMIT")
        for value in (
            self.lease_seconds,
            self.total_seconds,
            self.retry_seconds,
            self.dispatch_seconds,
        ):
            if not math.isfinite(value) or not 0 < value <= 86400:
                raise ValueError("JOB_LIMIT")
        if self.lease_seconds > 20 or self.retry_seconds > 180 or self.dispatch_seconds > 180:
            raise ValueError("JOB_LIMIT")


class UnavailablePolicy:
    async def check(self, command: ExecutionCommand, worker_id: str | None) -> ExecutionPolicy:
        from ai_agent_contracts.codec import ContractError

        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)


type Guard = Callable[[], None]
