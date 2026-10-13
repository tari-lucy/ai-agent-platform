"""Trusted resource configuration and local budget tokens, not worker credentials."""

import re
from dataclasses import dataclass
from enum import StrEnum

from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import Freshness
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.control.ports import ResourceTicket
from ai_agent_application.telemetry import Outcome


class Lane(StrEnum):
    MODERATION = "moderation"
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class BudgetState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    UNKNOWN = "unknown"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class Pool:
    id: str
    capacity: int
    reserved_for: Lane | None = None
    reserved_units: int = 0

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.id)
            or type(self.capacity) is not int
            or not 1 <= self.capacity <= 1000000
            or type(self.reserved_units) is not int
            or not 0 <= self.reserved_units <= self.capacity
            or (self.reserved_for is not None and not isinstance(self.reserved_for, Lane))
            or ((self.reserved_for is None) != (self.reserved_units == 0))
        ):
            raise ValueError("RESOURCE_POOL")


DEFAULT_POOLS = (
    Pool("llm", 4, Lane.MODERATION, 1),
    Pool("cpu", 2000, Lane.MODERATION, 200),
    Pool("ram", 1536, Lane.MODERATION, 128),
    Pool("background", 1),
    Pool("embedding", 1),
    Pool("foreground", 1),
)


@dataclass(frozen=True)
class Demand:
    pool_id: str
    units: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.pool_id):
            raise ValueError("RESOURCE_DEMAND")
        if type(self.units) is not int or not 1 <= self.units <= 1000000:
            raise ValueError("RESOURCE_DEMAND")


@dataclass(frozen=True)
class SchedulerLimits:
    pending: int = 256
    active: int = 64
    capabilities: int = 64
    pools: int = 32
    history: int = 256
    wait_seconds: float = 20

    def __post_init__(self) -> None:
        import math

        for value, maximum in (
            (self.pending, 256),
            (self.active, 200),
            (self.capabilities, 200),
            (self.pools, 64),
            (self.history, 256),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("SCHEDULER_LIMIT")
        if (
            type(self.wait_seconds) not in (int, float)
            or not math.isfinite(self.wait_seconds)
            or not 0 < self.wait_seconds <= 180
        ):
            raise ValueError("SCHEDULER_LIMIT")


@dataclass(frozen=True)
class Request:
    id: str
    operation_id: str
    attempt_id: str
    scope: Scope
    freshness: Freshness
    cancellation_id: str
    capability_id: str
    capability_version: int
    fingerprint: str
    deadline: float
    wait_deadline: float
    lane: Lane


@dataclass(frozen=True)
class BudgetLease:
    id: str
    request: Request
    ticket: ResourceTicket
    demands: tuple[Demand, ...]


@dataclass(frozen=True)
class Allocation:
    lease: BudgetLease
    acquired: bool


@dataclass(frozen=True)
class Decision:
    request: Request
    state: BudgetState
    error: ErrorCode | None = None
    outcome: Outcome | None = None
    wake_event: str | None = None


@dataclass(frozen=True)
class PoolUsage:
    pool: Pool
    used: int
    ordinary_used: int
