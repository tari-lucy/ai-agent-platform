"""Trusted composition ports. These objects are not client-supplied credentials."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from typing import Literal, Protocol

from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import Freshness, RunSession

type WorkKind = Literal["activity", "voice", "gm", "reflection", "observation", "dialogue"]


@dataclass(frozen=True)
class Clock:
    elapsed: Callable[[], float] = monotonic
    utc: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class ControlAuthority:
    """Issued by M1-05 AFTER session/Origin/CSRF checks; not an HTTP DTO."""

    principal_id: str
    partitions: frozenset[tuple[str, str, str, str]]
    expires_at: float
    # Optional process-local auth/session revocation. Never a client-supplied callback.
    is_current: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.expires_at):
            raise ValueError("CONTROL_AUTHORITY_EXPIRY")


@dataclass(frozen=True)
class ContextApproval:
    """Readiness owner checked profile, rights, schema and required connections.

    Includes fresh run/session; expiry is monotonic. Failed checks raise safe
    ContractError. This proof never substitutes for per-action permissions.
    """

    scope: Scope
    profile_version: int
    policy_version: int
    expires_at: float


class Readiness(Protocol):
    async def check(self, run: RunSession) -> ContextApproval: ...

    async def reconcile(self, previous: RunSession) -> tuple[str, ...]:
        """Unresolved resources; empty ONLY after confirmed reconciliation."""
        ...


class RunJournal(Protocol):
    async def load(self) -> list[RunSession]: ...

    async def save(self, snapshot: RunSession) -> None:
        """Atomic snapshot + owner receipt/outbox; never grants execution."""
        ...


@dataclass(frozen=True)
class Admission:
    id: str
    scope: Scope
    freshness: Freshness
    kind: WorkKind
    expires_at: float
    cancelled: Event


@dataclass(frozen=True)
class ResourceTicket:
    """Executor echoes exact ticket through its authenticated service binding."""

    id: str
    resource_id: str
    admission_id: str
    scope: Scope


@dataclass(frozen=True)
class ControlLimits:
    resources: int = 200
    admissions: int = 256
    requests: int = 256
    lease_seconds: float = 20.0
    stop_seconds: float = 2.0
    pause_seconds: float = 2.0
    sleep_gap_seconds: float = 5.0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int for value in (self.resources, self.admissions, self.requests)
        ):
            raise ValueError("CONTROL_LIMIT")
        if not (1 <= self.resources <= 200 and 1 <= self.admissions <= 256):
            raise ValueError("CONTROL_LIMIT")
        if not 1 <= self.requests <= 256:
            raise ValueError("CONTROL_LIMIT")
        for value in (
            self.lease_seconds,
            self.stop_seconds,
            self.pause_seconds,
            self.sleep_gap_seconds,
        ):
            if not math.isfinite(value) or not 0 < value <= 60:
                raise ValueError("CONTROL_LIMIT")
