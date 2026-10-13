"""Single-loop bounded operational memory. No raw error, prompt or provider payload API."""

import hashlib
import math
import re
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import get_ident
from uuid import uuid4

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.control.ports import Clock


class HealthState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class Metric(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    FINISHED = "finished"
    HEALTH = "health"
    RESOURCE_USED = "resource_used"
    CANCELLATION_REQUESTED = "cancellation_requested"
    SETTLED = "settled"


@dataclass(frozen=True)
class TelemetryLimits:
    components: int = 64
    latest: int = 256
    traces: int = 256
    clients: int = 32
    client_events: int = 256
    health_seconds: float = 20

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.components, 64),
            (self.latest, 256),
            (self.traces, 256),
            (self.clients, 32),
            (self.client_events, 256),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("TELEMETRY_LIMIT")
        if (
            type(self.health_seconds) not in (int, float)
            or not math.isfinite(self.health_seconds)
            or not 0 < self.health_seconds <= 60
        ):
            raise ValueError("TELEMETRY_LIMIT")


@dataclass(frozen=True)
class Health:
    component: str
    state: HealthState
    checked_at: float
    critical: bool
    error: ErrorCode | None = None


@dataclass(frozen=True)
class Trace:
    id: str
    scope_tag: str
    component: str
    request_tag: str | None
    operation_tag: str | None
    attempt_tag: str | None
    started_at: float


@dataclass(frozen=True)
class TraceResult:
    trace: Trace
    outcome: Outcome
    elapsed_seconds: float


@dataclass(frozen=True)
class Event:
    sequence: int
    scope_tag: str
    component: str
    metric: Metric
    value: int
    outcome: Outcome | None = None
    error: ErrorCode | None = None
    trace_id: str | None = None


class Subscription:
    def __init__(self, scope_tag: str, maximum: int) -> None:
        self.scope_tag = scope_tag
        self.maximum = maximum
        self._events: deque[Event] = deque()
        self.closed = False
        self.error: ErrorCode | None = None

    def _offer(self, event: Event) -> None:
        if len(self._events) >= self.maximum:
            self.close(ErrorCode.RESOURCE_BUSY)
        elif not self.closed:
            self._events.append(event)

    def close(self, error: ErrorCode | None = None) -> None:
        self.closed, self.error = True, error
        self._events.clear()

    def drain(self, maximum: int = 50) -> tuple[Event, ...]:
        if type(maximum) is not int or not 1 <= maximum <= 200:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if self.closed:
            raise ContractError(self.error or ErrorCode.STALE_CONTEXT)
        return tuple(self._events.popleft() for _ in range(min(maximum, len(self._events))))


def scope_tag(scope: Scope) -> str:
    # Scope/run/session isolation without putting profile names in diagnostics.
    return hashlib.sha256(scope.model_dump_json().encode()).hexdigest()


def _tag(identifier: str | None) -> str | None:
    if identifier is None:
        return None
    if type(identifier) is not str or not 1 <= len(identifier) <= 128:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    return hashlib.sha256(identifier.encode()).hexdigest()


class Telemetry:
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        limits: TelemetryLimits | None = None,
        critical_failure: Callable[[ErrorCode], None] | None = None,
    ) -> None:
        self.clock, self.limits = clock or Clock(), limits or TelemetryLimits()
        self._thread = get_ident()
        self._critical_failure = critical_failure
        self._health: dict[str, Health] = {}
        self._latest: OrderedDict[tuple[str, str, Metric], Event] = OrderedDict()
        self._active: dict[str, Trace] = {}
        self._finished: OrderedDict[str, TraceResult] = OrderedDict()
        self._clients: list[Subscription] = []
        self.sequence = 0
        self.dropped_latest = 0
        self.disconnected_clients = 0

    def _owned(self) -> None:
        if get_ident() != self._thread:
            raise RuntimeError("TELEMETRY_OWNER_THREAD_REQUIRED")

    def register(self, component: str, *, critical: bool = False) -> None:
        self._owned()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", component) or type(critical) is not bool:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        existing = self._health.get(component)
        if existing is not None:
            if existing.critical != critical:
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            return
        if len(self._health) >= self.limits.components:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        self._health[component] = Health(
            component,
            HealthState.UNAVAILABLE,
            self.clock.elapsed(),
            critical,
            ErrorCode.SERVICE_UNAVAILABLE,
        )

    def _component(self, component: str) -> Health:
        self._owned()
        if component not in self._health:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        return self._health[component]

    def update_health(
        self,
        component: str,
        state: HealthState,
        *,
        error: ErrorCode | None = None,
    ) -> None:
        prior = self._component(component)
        if not isinstance(state, HealthState) or (
            error is not None and not isinstance(error, ErrorCode)
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self._health[component] = Health(
            component, state, self.clock.elapsed(), prior.critical, error
        )
        if state == HealthState.UNAVAILABLE and prior.critical and self._critical_failure:
            self._critical_failure(error or ErrorCode.SERVICE_UNAVAILABLE)

    def poll(self) -> None:
        self._owned()
        now = self.clock.elapsed()
        for component, health in tuple(self._health.items()):
            age = now - health.checked_at
            if health.state != HealthState.UNAVAILABLE and (
                age < 0 or age >= self.limits.health_seconds
            ):
                self.update_health(
                    component, HealthState.UNAVAILABLE, error=ErrorCode.SERVICE_UNAVAILABLE
                )
        self._clients[:] = [client for client in self._clients if not client.closed]

    def health(self, component: str) -> Health:
        self.poll()
        return self._component(component)

    def health_snapshot(self) -> tuple[Health, ...]:
        self.poll()
        return tuple(self._health.values())

    def begin(
        self,
        scope: Scope,
        component: str,
        *,
        request_id: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Trace:
        self._component(component)
        if len(self._active) >= self.limits.traces:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        trace = Trace(
            uuid4().hex,
            scope_tag(scope),
            component,
            _tag(request_id),
            _tag(operation_id),
            _tag(attempt_id),
            self.clock.elapsed(),
        )
        self._active[trace.id] = trace
        return trace

    def end(self, trace: Trace, outcome: Outcome) -> TraceResult:
        self._owned()
        if not isinstance(outcome, Outcome):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        finished = self._finished.get(trace.id)
        if finished is not None:
            if finished.trace is not trace or finished.outcome != outcome:
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            return finished
        if self._active.get(trace.id) is not trace:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        duration = max(0.0, self.clock.elapsed() - trace.started_at)
        result = TraceResult(trace, outcome, duration)
        del self._active[trace.id]
        self._finished[trace.id] = result
        if len(self._finished) > self.limits.traces:
            self._finished.popitem(last=False)
        self.emit_tag(
            trace.scope_tag, trace.component, Metric.FINISHED, 1, outcome=outcome, trace_id=trace.id
        )
        return result

    def emit(
        self,
        scope: Scope,
        component: str,
        metric: Metric,
        value: int,
        *,
        outcome: Outcome | None = None,
        error: ErrorCode | None = None,
    ) -> None:
        self.emit_tag(scope_tag(scope), component, metric, value, outcome=outcome, error=error)

    def emit_tag(
        self,
        partition_tag: str,
        component: str,
        metric: Metric,
        value: int,
        *,
        outcome: Outcome | None = None,
        error: ErrorCode | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._component(component)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", partition_tag)
            or not isinstance(metric, Metric)
            or type(value) is not int
            or not 0 <= value <= 1000000
            or (outcome is not None and not isinstance(outcome, Outcome))
            or (error is not None and not isinstance(error, ErrorCode))
            or (trace_id is not None and not re.fullmatch(r"[a-f0-9]{32}", trace_id))
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self.sequence += 1
        event = Event(
            self.sequence, partition_tag, component, metric, value, outcome, error, trace_id
        )
        key = (partition_tag, component, metric)
        self._latest[key] = event
        self._latest.move_to_end(key)
        if len(self._latest) > self.limits.latest:
            self._latest.popitem(last=False)
            self.dropped_latest += 1
        for client in tuple(self._clients):
            if not client.closed and client.scope_tag == partition_tag:
                client._offer(event)
                if client.closed:
                    self.disconnected_clients += 1
        self._clients[:] = [client for client in self._clients if not client.closed]

    def snapshot(self, scope: Scope) -> tuple[Event, ...]:
        self._owned()
        tag = scope_tag(scope)
        return tuple(event for event in self._latest.values() if event.scope_tag == tag)

    def traces(self, scope: Scope) -> tuple[TraceResult, ...]:
        self._owned()
        tag = scope_tag(scope)
        return tuple(item for item in self._finished.values() if item.trace.scope_tag == tag)

    def subscribe(self, scope: Scope) -> Subscription:
        # Trusted composition must authenticate/authorize the scope before calling.
        self.poll()
        if len(self._clients) >= self.limits.clients:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        client = Subscription(scope_tag(scope), self.limits.client_events)
        self._clients.append(client)
        return client

    def unsubscribe(self, client: Subscription) -> None:
        self._owned()
        client.close()
        self._clients[:] = [item for item in self._clients if item is not client]
