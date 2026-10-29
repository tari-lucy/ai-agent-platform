"""Synchronous budget owner. No IO/await in allocation, revocation observation or release."""

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from threading import get_ident
from uuid import uuid4

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Id, Scope
from ai_agent_contracts.control import ExecutionCommand
from ai_agent_contracts.states import ErrorCode, RunState
from ai_agent_contracts.workspace import Capability
from pydantic import TypeAdapter, ValidationError

from ai_agent_application.control import Supervisor
from ai_agent_application.telemetry import HealthState, Metric, Outcome, Telemetry
from ai_agent_application.telemetry.service import Trace

from .model import (
    DEFAULT_POOLS,
    Allocation,
    BudgetLease,
    BudgetState,
    Decision,
    Demand,
    Lane,
    Pool,
    PoolUsage,
    Request,
    SchedulerLimits,
)


@dataclass(frozen=True)
class _Capability:
    version: int
    partition: tuple[str, str, str, str]
    demands: tuple[Demand, ...]
    lane: Lane
    components: tuple[str, ...]
    health: HealthState


@dataclass
class _Work:
    request: Request
    trace: Trace
    lease: BudgetLease | None = None
    state: BudgetState = BudgetState.WAITING
    error: ErrorCode | None = None
    trace_finished: bool = False
    cancellation_at: float | None = None


class ResourceScheduler:
    def __init__(
        self,
        supervisor: Supervisor,
        telemetry: Telemetry,
        *,
        pools: tuple[Pool, ...] = DEFAULT_POOLS,
        limits: SchedulerLimits | None = None,
    ) -> None:
        if (
            supervisor.snapshot.state != RunState.STOPPED
            or supervisor.snapshot.unconfirmed_resource_ids
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.supervisor, self.telemetry = supervisor, telemetry
        self.clock = supervisor.clock
        self.limits = limits or SchedulerLimits()
        if not pools or len(pools) > self.limits.pools or len({p.id for p in pools}) != len(pools):
            raise ValueError("RESOURCE_POOLS")
        self._pools = {pool.id: pool for pool in pools}
        self._registry: dict[str, _Capability] = {}
        self._health_states: dict[str, HealthState] = {}
        self._pending: OrderedDict[str, _Work] = OrderedDict()
        self._active: dict[str, _Work] = {}
        self._history: OrderedDict[str, tuple[_Work, Decision]] = OrderedDict()
        self._used = dict.fromkeys(self._pools, 0)
        self._ordinary = dict.fromkeys(self._pools, 0)
        self._thread = get_ident()
        telemetry.register("resource_scheduler")
        telemetry.update_health("resource_scheduler", HealthState.READY)

    def _owned(self) -> None:
        if get_ident() != self._thread:
            raise RuntimeError("SCHEDULER_OWNER_THREAD_REQUIRED")

    def register(
        self,
        capability: Capability,
        demands: tuple[Demand, ...],
        lane: Lane,
        *,
        components: tuple[str, ...] = (),
    ) -> None:
        """Trusted core registry configuration. A client cannot choose its resource lane."""
        self._owned()
        if (
            not isinstance(lane, Lane)
            or len(demands) > self.limits.pools
            or len({d.pool_id for d in demands}) != len(demands)
            or {d.pool_id for d in demands} != set(capability.resource_ids)
            or len(components) > self.telemetry.limits.components
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if capability.scope.partition != self.supervisor.snapshot.scope.partition:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if capability.id not in self._registry and len(self._registry) >= self.limits.capabilities:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        for demand in demands:
            pool = self._pools.get(demand.pool_id)
            if pool is None or demand.units > pool.capacity:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            if pool.reserved_for is not None:
                if lane == pool.reserved_for and demand.units > pool.reserved_units:
                    # Guarantee the reserved lane also fits its CPU/RAM reservations.
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
                if lane != pool.reserved_for and demand.units > pool.capacity - pool.reserved_units:
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        for component in components:
            self.telemetry.health(component)  # must already be a registered health owner
        prior = self._registry.get(capability.id)
        specification = _Capability(
            capability.version,
            capability.scope.partition,
            demands,
            lane,
            components,
            HealthState(capability.health),
        )
        if prior is not None and capability.version <= prior.version and prior != specification:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        self._registry[capability.id] = specification

    def _specification(self, request: Request) -> _Capability:
        specification = self._registry.get(request.capability_id)
        if specification is None or specification.partition != request.scope.partition:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if specification.version != request.capability_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        return specification

    def _guard(self, request: Request, *, waiting: bool = False) -> None:
        self.supervisor.resolve(
            request.cancellation_id, scope=request.scope, freshness=request.freshness
        )
        self._specification(request)
        now = self.clock.elapsed()
        if now >= request.deadline or (waiting and now >= request.wait_deadline):
            raise ContractError(ErrorCode.EXPIRED)

    def _healthy(self, specification: _Capability) -> bool:
        return specification.health != HealthState.UNAVAILABLE and all(
            self._health_states.get(component, HealthState.UNAVAILABLE) != HealthState.UNAVAILABLE
            for component in specification.components
        )

    def _metrics(self, scope: Scope) -> None:
        pending = sum(work.request.scope == scope for work in self._pending.values())
        active = sum(work.request.scope == scope for work in self._active.values())
        self.telemetry.emit(scope, "resource_scheduler", Metric.QUEUED, pending)
        self.telemetry.emit(scope, "resource_scheduler", Metric.RUNNING, active)

    def _finish_trace(self, work: _Work, outcome: Outcome) -> None:
        if not work.trace_finished:
            self.telemetry.end(work.trace, outcome)
            work.trace_finished = True

    def _remember(self, work: _Work, outcome: Outcome, error: ErrorCode | None = None) -> None:
        decision = Decision(work.request, BudgetState.TERMINAL, error, outcome)
        self._history[work.request.id] = (work, decision)
        if len(self._history) > self.limits.history:
            self._history.popitem(last=False)
        self._finish_trace(work, outcome)
        self._metrics(work.request.scope)

    def poll(self) -> None:
        self._owned()
        self.supervisor.poll()
        self.telemetry.poll()
        self.telemetry.update_health("resource_scheduler", HealthState.READY)
        self._health_states = {
            health.component: health.state for health in self.telemetry.health_snapshot()
        }
        for identifier, work in tuple(self._pending.items()):
            try:
                self._guard(work.request, waiting=True)
            except ContractError as error:
                del self._pending[identifier]
                outcome = (
                    Outcome.EXPIRED if error.error.code == ErrorCode.EXPIRED else Outcome.CANCELLED
                )
                self._remember(work, outcome, error.error.code)
        for work in tuple(self._active.values()):
            try:
                self._guard(work.request)
                healthy = self._healthy(self._specification(work.request))
                if not healthy:
                    raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            except ContractError as error:
                if work.state == BudgetState.RUNNING:
                    # Diagnostics: the running job loses its resources here, silently otherwise.
                    logging.getLogger(__name__).warning(
                        "Resource lease cancelled: %s capability=%s",
                        error.error.code.value,
                        work.request.capability_id,
                    )
                    work.state, work.error = BudgetState.CANCELLING, error.error.code
                    work.cancellation_at = self.clock.elapsed()
                    self.telemetry.emit(
                        work.request.scope,
                        "resource_scheduler",
                        Metric.CANCELLATION_REQUESTED,
                        1,
                        error=error.error.code,
                    )
            if (
                work.state == BudgetState.CANCELLING
                and work.cancellation_at is not None
                and self.clock.elapsed() - work.cancellation_at
                >= self.supervisor.limits.stop_seconds
            ):
                work.state, work.error = BudgetState.UNKNOWN, ErrorCode.OUTCOME_UNKNOWN
                self._finish_trace(work, Outcome.UNKNOWN)
                # No freeing resources or acknowledging supervisor on mere expiry/STOP.

    def request(
        self,
        command: ExecutionCommand,
        *,
        attempt_id: str | None = None,
    ) -> Request:
        self.poll()
        try:
            attempt = TypeAdapter(Id).validate_python(
                command.id if attempt_id is None else attempt_id
            )
        except ValidationError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        specification = self._registry.get(command.capability_id)
        if specification is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        fingerprint = hashlib.sha256(
            json.dumps(
                command.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        # Run/session and work/test are part of this exact identity.
        for work in (
            *self._pending.values(),
            *self._active.values(),
            *(item[0] for item in self._history.values()),
        ):
            prior = work.request
            if prior.operation_id == command.operation_id and prior.scope == command.scope:
                if prior.attempt_id == attempt:
                    if prior.fingerprint != fingerprint:
                        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
                    return prior  # completed attempt never restarts automatically
                if prior.id in self._pending or prior.id in self._active:
                    code = (
                        ErrorCode.OUTCOME_UNKNOWN
                        if work.state == BudgetState.UNKNOWN
                        else ErrorCode.RESOURCE_BUSY
                    )
                    raise ContractError(code)
        if len(self._pending) >= self.limits.pending:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        # D76: a long work turn holds its resources up to its job deadline (24 h rail).
        remaining = min(86400.0, (command.deadline - self.clock.utc()).total_seconds())
        now = self.clock.elapsed()
        request = Request(
            uuid4().hex,
            command.operation_id,
            attempt,
            command.scope,
            command.freshness,
            command.cancellation_id,
            command.capability_id,
            command.capability_version,
            fingerprint,
            now + remaining,
            now + min(remaining, self.limits.wait_seconds),
            specification.lane,
        )
        self._guard(request, waiting=True)
        trace = self.telemetry.begin(
            command.scope,
            "resource_scheduler",
            request_id=command.id,
            operation_id=command.operation_id,
            attempt_id=attempt,
        )
        self._pending[request.id] = _Work(request, trace)
        self._metrics(request.scope)
        return request

    def _fits(self, specification: _Capability) -> bool:
        for demand in specification.demands:
            pool = self._pools[demand.pool_id]
            if self._used[pool.id] + demand.units > pool.capacity:
                return False
            if specification.lane != pool.reserved_for and (
                self._ordinary[pool.id] + demand.units > pool.capacity - pool.reserved_units
            ):
                return False
        return True

    def acquire(self, request: Request) -> Allocation | None:
        self.poll()
        active = self._active.get(request.id)
        if active is not None:
            if active.request is not request:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            # Duplicate budget claim is a status query, not a new execution permission.
            assert active.lease is not None
            self.verify(active.lease)
            return Allocation(active.lease, acquired=False)
        work = self._pending.get(request.id)
        if work is None or work.request is not request:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if len(self._active) >= self.limits.active:
            return None
        priority = {Lane.MODERATION: 0, Lane.FOREGROUND: 1, Lane.BACKGROUND: 2}
        ordered = sorted(self._pending.values(), key=lambda item: priority[item.request.lane])
        chosen: _Work | None = None
        for candidate in ordered:
            specification = self._specification(candidate.request)
            if self._healthy(specification) and self._fits(specification):
                chosen = candidate
                break
        if chosen is not work:
            return None
        specification = self._specification(request)
        admission = self.supervisor.resolve(
            request.cancellation_id, scope=request.scope, freshness=request.freshness
        )
        ticket = self.supervisor.register(admission, "budget-" + request.id)
        lease = BudgetLease(uuid4().hex, request, ticket, specification.demands)
        for demand in lease.demands:
            pool = self._pools[demand.pool_id]
            self._used[pool.id] += demand.units
            if request.lane != pool.reserved_for:
                self._ordinary[pool.id] += demand.units
        del self._pending[request.id]
        work.lease, work.state = lease, BudgetState.RUNNING
        self._active[request.id] = work
        self._metrics(request.scope)
        return Allocation(lease, acquired=True)

    def verify(self, lease: BudgetLease) -> None:
        """Caller must ALSO verify current JobOwner/Policy immediately before IO/effect."""
        self.poll()
        work = self._active.get(lease.request.id)
        if work is None or work.lease is not lease or work.state != BudgetState.RUNNING:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self._guard(lease.request)
        if not self._healthy(self._specification(lease.request)):
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        self._guard(lease.request)

    def complete(self, lease: BudgetLease, outcome: Outcome) -> Decision:
        """Trusted, authenticated actual executor completion/cancel/reconciliation receipt."""
        self.poll()
        if not isinstance(outcome, Outcome):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        work = self._active.get(lease.request.id)
        if work is None:
            prior = self._history.get(lease.request.id)
            if prior is None or prior[0].lease is not lease:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if prior[1].outcome != outcome:
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            return prior[1]
        if work.lease is not lease:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if outcome == Outcome.UNKNOWN:
            work.state = BudgetState.UNKNOWN
            work.error = ErrorCode.OUTCOME_UNKNOWN
            self._finish_trace(work, outcome)
            return Decision(work.request, work.state, work.error, outcome)
        # Late factual receipts may release budgets after STOP, never authorize an effect.
        self.supervisor.acknowledge(lease.ticket)
        for demand in lease.demands:
            pool = self._pools[demand.pool_id]
            self._used[pool.id] -= demand.units
            if lease.request.lane != pool.reserved_for:
                self._ordinary[pool.id] -= demand.units
        del self._active[lease.request.id]
        if work.trace_finished:
            self.telemetry.emit(
                work.request.scope, "resource_scheduler", Metric.SETTLED, 1, outcome=outcome
            )
        self._remember(work, outcome)
        return self._history[work.request.id][1]

    def cancel_waiting(self, request: Request) -> Decision:
        self.poll()
        work = self._pending.get(request.id)
        if work is None or work.request is not request:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        del self._pending[request.id]
        self._remember(work, Outcome.CANCELLED)
        return self._history[request.id][1]

    def decision(self, request: Request) -> Decision:
        self.poll()
        work = self._pending.get(request.id) or self._active.get(request.id)
        if work is not None:
            if work.request is not request:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if work.state == BudgetState.WAITING:
                healthy = self._healthy(self._specification(request))
                reason = ErrorCode.RESOURCE_BUSY if healthy else ErrorCode.CAPABILITY_UNAVAILABLE
                return Decision(
                    request, work.state, reason, wake_event="scheduler.capacity_or_health"
                )
            return Decision(request, work.state, work.error)
        previous = self._history.get(request.id)
        if previous is None or previous[0].request is not request:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return previous[1]

    def usage(self) -> tuple[PoolUsage, ...]:
        self.poll()
        return tuple(
            PoolUsage(pool, self._used[pool.id], self._ordinary[pool.id])
            for pool in self._pools.values()
        )
