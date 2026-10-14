"""Single core job owner. Broker delivery never grants execution."""

import asyncio
import hashlib
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

from ai_agent_contracts.api import (
    EditCommand,
    ServiceClaimCommand,
    ServiceLeaseCommand,
    ServiceResultCommand,
)
from ai_agent_contracts.codec import ContractError, to_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import ExecutionCommand, ExecutionResult, Job, Lease
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import ErrorCode, OperationState

from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import Admission, Clock, ControlAuthority, ResourceTicket

from .ports import (
    Attempt,
    AttemptStatus,
    Broker,
    ExecutionPolicy,
    JobLimits,
    JobStore,
    Policy,
    Schedule,
    WorkerAuthority,
)

ATTEMPT_STATUS: dict[str, AttemptStatus] = {
    "SUCCEEDED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
    "OUTCOME_UNKNOWN": "unknown",
}
TERMINAL = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.REJECTED,
        OperationState.CANCELLED,
        OperationState.EXPIRED,
        OperationState.OUTCOME_UNKNOWN,
    }
)


@dataclass(frozen=True)
class Claim:
    command: ExecutionCommand
    lease: Lease
    acquired: bool = True


class JobOwner:
    def __init__(
        self,
        store: JobStore,
        supervisor: Supervisor,
        policy: Policy,
        *,
        limits: JobLimits | None = None,
        clock: Clock | None = None,
        extensions: ExtensionRegistry | None = None,
        jitter: Callable[[], float] = lambda: random.uniform(0.75, 1.25),
    ) -> None:
        self.store, self.supervisor, self.policy = store, supervisor, policy
        self.extensions = extensions
        self.jitter = jitter
        self.limits, self.clock = limits or JobLimits(), clock or supervisor.clock
        self._lock = asyncio.Lock()
        self._reconcile_cursor: str | None = None
        self._deadlines: dict[str, float] = {}
        self._leases: dict[str, float] = {}
        self._tickets: dict[str, ResourceTicket] = {}

    def _worker(self, authority: WorkerAuthority, scope: Scope, capability: str) -> None:
        if (
            not math.isfinite(authority.expires_at)
            or self.clock.elapsed() >= authority.expires_at
            or scope.partition not in authority.partitions
            or capability not in authority.capability_ids
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def _admission(self, command: ExecutionCommand) -> Admission:
        if self.clock.utc() >= command.deadline:
            raise ContractError(ErrorCode.EXPIRED)
        return self.supervisor.resolve(
            command.cancellation_id,
            scope=command.scope,
            freshness=command.freshness,
        )

    async def _proof(
        self,
        command: ExecutionCommand,
        worker_id: str | None,
    ) -> ExecutionPolicy:
        proof = await self.policy.check(command, worker_id)
        if (
            proof.command_id != command.id
            or proof.scope != command.scope
            or not math.isfinite(proof.expires_at)
            or proof.expires_at <= self.clock.elapsed()
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self._admission(command)
        return proof

    def _proof_current(self, proof: ExecutionPolicy, command: ExecutionCommand) -> None:
        if self.clock.elapsed() >= proof.expires_at:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self._admission(command)

    async def _load(self, operation: str, scope: Scope) -> tuple[Job, Schedule]:
        stored = await self.store.load(operation, scope)
        if stored is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return stored

    def _edit(self, job: Job, **values: object) -> Job:
        return job.model_copy(
            deep=True,
            update={
                **values,
                "version": job.version + 1,
                "updated_at": max(self.clock.utc(), job.updated_at),
            },
        )

    def _command(self, job: Job, request_id: str) -> ServiceClaimCommand:
        return ServiceClaimCommand(
            request_id=request_id,
            type="job.claim",
            scope=job.scope,
            target=job.operation_id,
            operation_id=job.operation_id,
            expected_version=job.version,
            freshness=job.freshness,
        )

    async def _save(
        self,
        job: Job,
        schedule: Schedule,
        request: ServiceClaimCommand | ServiceLeaseCommand | ServiceResultCommand | EditCommand,
        *,
        previous: Job | None,
        attempt: Attempt | None = None,
        result: ExecutionResult | None = None,
        reconciliation: ExecutionResult | None = None,
    ) -> None:
        async with self.store.transaction(request, pending_limit=self.limits.pending) as tx:
            await tx.save(
                job,
                schedule,
                expected_version=previous.version if previous else None,
                attempt=attempt,
                result=result,
                reconciliation=reconciliation,
            )

    async def submit(self, command_id: str, scope: Scope, *, goal: str) -> Job:
        """Trusted behavior owner already durably wrote ExecutionCommand.

        No worker/client can create a command or replace its cancellation ID here.
        """
        async with self._lock:
            command = await self.store.command(command_id, scope)
            fingerprint = hashlib.sha256(
                to_wire(command, extensions=self.extensions) + goal.encode()
            ).hexdigest()
            old = await self.store.load(command.operation_id, scope)
            if old is not None:
                if old[1].fingerprint != fingerprint:
                    raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return old[0]  # Known status only, never execution permission.
            proof = await self._proof(command, None)
            now = self.clock.utc()
            remaining = min((command.deadline - now).total_seconds(), self.limits.total_seconds)
            if remaining <= 0:
                raise ContractError(ErrorCode.EXPIRED)
            job = Job.model_validate(
                dict(
                    id=command.operation_id,
                    operation_id=command.operation_id,
                    command_id=command.id,
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    goal=goal,
                    capability_id=command.capability_id,
                    capability_version=command.capability_version,
                    status=OperationState.QUEUED,
                    deadline=min(command.deadline, now + timedelta(seconds=remaining)),
                    conditions=command.conditions,
                    freshness=command.freshness,
                ),
                context={"extensions": self.extensions},
            )
            schedule = Schedule(
                job.id, job.operation_id, scope, fingerprint, 0, now, proof.retry_safe
            )
            self._proof_current(proof, command)
            await self._save(
                job,
                schedule,
                self._command(job, "submit-" + uuid4().hex),
                previous=None,
            )
            self._deadlines[job.operation_id] = self.clock.elapsed() + remaining
            # A durable queued row is a technical fact, not an admission.
            return job

    def _deadline(self, job: Job) -> None:
        if self.clock.utc() >= job.deadline or self.clock.elapsed() >= self._deadlines.get(
            job.operation_id, -math.inf
        ):
            raise ContractError(ErrorCode.EXPIRED)

    def _lease(self, job: Job, lease: Lease, authority: WorkerAuthority) -> None:
        self._worker(authority, job.scope, job.capability_id)
        current = job.lease
        if (
            current is None
            or job.status != OperationState.RUNNING
            or lease.scope != job.scope
            or lease.freshness != job.freshness
            or lease.command_id != job.command_id
            or lease.operation_id != job.operation_id
            or (lease.attempt_id, lease.lease_version, lease.worker_id)
            != (
                current.attempt_id,
                current.lease_version,
                authority.id,
            )
            or self.clock.utc() >= current.expires_at
            or self.clock.elapsed() >= self._leases.get(current.attempt_id, -math.inf)
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self._deadline(job)

    async def claim(self, authority: WorkerAuthority, request: ServiceClaimCommand) -> Claim | None:
        async with self._lock:
            job, schedule = await self._load(request.operation_id, request.scope)
            self._worker(authority, job.scope, job.capability_id)
            await self.store.receipt(request)
            if request.freshness != job.freshness:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if job.status in TERMINAL:
                return None  # Delivery may be ACKed; no permission issued.
            if job.status == OperationState.RUNNING:
                # Replayed claim never issues a second attempt or resurrects a lease.
                if job.lease and job.lease.worker_id == authority.id:
                    self._lease(job, job.lease, authority)
                    command = await self.store.command(job.command_id, job.scope)
                    await self._proof(command, authority.id)
                    self._lease(job, job.lease, authority)
                    return Claim(command, job.lease, acquired=False)
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            if job.version != request.expected_version:
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            if job.status not in {OperationState.QUEUED, OperationState.WAITING}:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            self._deadline(job)
            if schedule.ready_at > self.clock.utc() or schedule.attempts >= self.limits.attempts:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            command = await self.store.command(job.command_id, job.scope)
            proof = await self._proof(command, authority.id)
            if schedule.attempts > 0 and not proof.retry_safe:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            token = self._admission(command)
            lease_end = min(
                token.expires_at,
                self.clock.elapsed() + self.limits.lease_seconds,
                self._deadlines[job.operation_id],
            )
            lease = Lease(
                operation_id=job.operation_id,
                command_id=job.command_id,
                attempt_id=uuid4().hex,
                lease_version=schedule.attempts + 1,
                worker_id=authority.id,
                scope=job.scope,
                freshness=job.freshness,
                expires_at=min(
                    job.deadline,
                    self.clock.utc()
                    + timedelta(
                        seconds=min(
                            self.limits.lease_seconds, token.expires_at - self.clock.elapsed()
                        ),
                    ),
                ),
            )
            updated = self._edit(
                job,
                status=OperationState.RUNNING,
                lease=lease,
                waiting_reason=None,
                wake_event=None,
            )
            schedule = replace(schedule, attempts=schedule.attempts + 1)
            self._proof_current(proof, command)
            await self._save(
                updated,
                schedule,
                request,
                previous=job,
                attempt=Attempt(lease, "running"),
            )
            self._proof_current(proof, command)
            self._worker(authority, job.scope, job.capability_id)
            self._deadline(updated)
            if self.clock.utc() >= lease.expires_at or self.clock.elapsed() >= lease_end:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            # Persist first. No lease expiry is interpreted as resource cancellation.
            self._leases[lease.attempt_id] = lease_end
            self._tickets[lease.attempt_id] = self.supervisor.register(
                token,
                "job-" + lease.attempt_id,
            )
            return Claim(command, lease)

    async def renew(self, authority: WorkerAuthority, request: ServiceLeaseCommand) -> Lease:
        if request.type != "job.renew":
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        async with self._lock:
            job, schedule = await self._load(request.operation_id, request.scope)
            self._lease(job, request.lease, authority)
            if await self.store.receipt(request) is not None:
                self._lease(job, request.lease, authority)
                command = await self.store.command(job.command_id, job.scope)
                await self._proof(command, authority.id)
                self._lease(job, request.lease, authority)
                assert job.lease is not None
                return job.lease
            command = await self.store.command(job.command_id, job.scope)
            proof = await self._proof(command, authority.id)
            token = await self.supervisor.refresh(self._admission(command))
            self._lease(job, request.lease, authority)
            self._proof_current(proof, command)
            lease = request.lease.model_copy(
                update={
                    "expires_at": min(
                        job.deadline,
                        self.clock.utc()
                        + timedelta(
                            seconds=min(
                                self.limits.lease_seconds, token.expires_at - self.clock.elapsed()
                            ),
                        ),
                    ),
                }
            )
            updated = self._edit(job, lease=lease)
            await self._save(
                updated,
                schedule,
                request,
                previous=job,
                attempt=Attempt(lease, "running"),
            )
            self._lease(job, request.lease, authority)  # Reject old expiry after slow DB write.
            self._proof_current(proof, command)
            self._leases[lease.attempt_id] = min(
                token.expires_at,
                self.clock.elapsed() + self.limits.lease_seconds,
                self._deadlines[job.operation_id],
            )
            return lease

    def _ack_resource(self, result: ExecutionResult) -> None:
        if result.status != "OUTCOME_UNKNOWN":
            ticket = self._tickets.pop(result.attempt_id, None)
            if ticket:
                self.supervisor.acknowledge(ticket)

    async def accept(
        self,
        authority: WorkerAuthority,
        request: ServiceResultCommand,
        *,
        transient: bool = False,
        error: ErrorCode | None = None,
    ) -> Job:
        """Persists technical computation outcome; never publishes/applies payload.

        Final consumer must check original cancellation_id AND its own rights,
        sources, exact revision and lease. Result/usage metadata after STOP is
        allowed; this method cannot reopen the supervisor.
        """
        async with self._lock:
            result = request.result
            job, schedule = await self._load(result.operation_id, result.scope)
            self._worker(authority, job.scope, job.capability_id)
            lease = job.lease
            if (
                lease is None
                or result.command_id != job.command_id
                or result.freshness != job.freshness
                or result.scope != job.scope
                or result.attempt_id != lease.attempt_id
                or result.lease_version != lease.lease_version
                or lease.worker_id != authority.id
                or result.completed_at > self.clock.utc()
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            await self.store.receipt(request)
            prior = await self.store.result(result.id, result.scope)
            if prior is not None:
                if to_wire(prior, extensions=self.extensions) != to_wire(
                    result, extensions=self.extensions
                ):
                    raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
                self._ack_resource(result)
                if job.status in TERMINAL:
                    self._leases.pop(lease.attempt_id, None)
                    self._deadlines.pop(job.operation_id, None)
                return job
            if job.status in TERMINAL:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            live = True
            command: ExecutionCommand | None = None
            try:
                self._lease(job, lease, authority)
                command = await self.store.command(job.command_id, job.scope)
                proof = await self._proof(command, authority.id)
                self._lease(job, lease, authority)
                self._proof_current(proof, command)
            except ContractError:
                live = False
            status = OperationState(result.status)
            ready_at = schedule.ready_at
            if result.status == "OUTCOME_UNKNOWN":
                status = OperationState.OUTCOME_UNKNOWN
            elif not live:
                status = OperationState.CANCELLED
            elif (
                transient and result.status == "FAILED" and schedule.retry_safe and proof.retry_safe
            ):
                if schedule.attempts < self.limits.attempts:
                    status = OperationState.WAITING
                    ready_at = self.clock.utc() + timedelta(
                        seconds=self.limits.retry_seconds * min(1.25, max(0.75, self.jitter()))
                    )
            values: dict[str, object] = {"status": status}
            if status == OperationState.WAITING:
                values.update(waiting_reason="bounded retry", wake_event="retry.timer")
            updated = self._edit(job, **values)
            schedule = replace(schedule, ready_at=ready_at)
            attempt = Attempt(
                lease,
                ATTEMPT_STATUS[result.status],
                self.clock.utc(),
                error,
            )
            await self._save(
                updated, schedule, request, previous=job, attempt=attempt, result=result
            )
            self._ack_resource(result)
            # This is a technical journal, not a DB+external-effect transaction.
            if status in TERMINAL:
                self._deadlines.pop(job.operation_id, None)
                self._leases.pop(lease.attempt_id, None)
            self._leases.pop(lease.attempt_id, None)
            return updated

    async def cancel(
        self, authority: ControlAuthority, request: EditCommand, operation_id: str
    ) -> Job:
        """Revoke one admission before IO; running execution needs a factual receipt."""
        async with self._lock:
            self.supervisor.authorize_operator(authority, request.scope)
            job, schedule = await self._load(operation_id, request.scope)
            known = await self.store.receipt(request)
            if known is not None:
                return job
            if request.expected_version != job.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            if job.scope != request.scope or job.status in TERMINAL:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            command = await self.store.command(job.command_id, job.scope)
            # Begin checks current SQL ACL before revocation, under the write reservation.
            async with self.store.transaction(request, pending_limit=self.limits.pending) as tx:
                token = self._admission(command)
                self.supervisor.retire(token)
                status = (
                    OperationState.OUTCOME_UNKNOWN
                    if job.status == OperationState.RUNNING
                    else OperationState.CANCELLED
                )
                updated = self._edit(job, status=status)
                await tx.save(updated, schedule, expected_version=job.version)
            return updated

    async def cancel_receipt(
        self,
        authority: WorkerAuthority,
        request: ServiceLeaseCommand,
    ) -> Job:
        """Factual termination ACK, including an old reclaimed executor.

        Expiration alone never calls this. A trusted authenticated executor must
        report that it has actually stopped. It cannot cancel the new attempt.
        """
        if request.type != "job.cancel_receipt":
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        async with self._lock:
            job, schedule = await self._load(request.operation_id, request.scope)
            self._worker(authority, job.scope, job.capability_id)
            previous = await self.store.attempt(
                job.operation_id, request.lease.attempt_id, job.scope
            )
            lease = request.lease
            if (
                previous is None
                or lease.scope != job.scope
                or lease.freshness != job.freshness
                or lease.command_id != job.command_id
                or previous.lease.worker_id != authority.id
                or lease.worker_id != authority.id
                or lease.lease_version != previous.lease.lease_version
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            known = await self.store.receipt(request)
            if known is not None:
                ticket = self._tickets.pop(lease.attempt_id, None)
                if ticket:
                    self.supervisor.acknowledge(ticket)
                return job
            values: dict[str, object] = {}
            if (
                job.lease
                and job.lease.attempt_id == lease.attempt_id
                and job.status not in TERMINAL
            ):
                values["status"] = OperationState.CANCELLED
            updated = self._edit(job, **values)
            await self._save(
                updated,
                schedule,
                request,
                previous=job,
                attempt=(
                    Attempt(previous.lease, "cancelled", self.clock.utc())
                    if previous.status in {"running", "unknown"}
                    else previous
                ),
            )
            ticket = self._tickets.pop(lease.attempt_id, None)
            if ticket:
                self.supervisor.acknowledge(ticket)
            self._leases.pop(lease.attempt_id, None)
            if updated.status in TERMINAL:
                self._deadlines.pop(job.operation_id, None)
            return updated

    async def verify(self, authority: WorkerAuthority, lease: Lease) -> ExecutionCommand:
        """Check immediately before executor I/O; loss of core means fail closed."""
        async with self._lock:
            job, _ = await self._load(lease.operation_id, lease.scope)
            self._lease(job, lease, authority)
            command = await self.store.command(job.command_id, job.scope)
            proof = await self._proof(command, authority.id)
            self._lease(job, lease, authority)
            self._proof_current(proof, command)
            return command

    async def settle_unknown(
        self,
        authority: WorkerAuthority,
        request: ServiceResultCommand,
    ) -> Job:
        """Authenticated factual receipt only; never authorizes a new attempt.

        Preserve original unknown result and append metadata-only reconciliation.
        Expired run/lease is expected here. The exact old worker/attempt is still
        required; source access is checked by persistence. No result payload.
        """
        async with self._lock:
            result = request.result
            job, schedule = await self._load(result.operation_id, result.scope)
            self._worker(authority, job.scope, job.capability_id)
            lease = job.lease
            if (
                lease is None
                or result.result is not None
                or result.status == "OUTCOME_UNKNOWN"
                or result.completed_at > self.clock.utc()
                or result.command_id != job.command_id
                or result.freshness != job.freshness
                or result.attempt_id != lease.attempt_id
                or result.lease_version != lease.lease_version
                or lease.worker_id != authority.id
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            fingerprint = hashlib.sha256(to_wire(result, extensions=self.extensions)).hexdigest()
            await self.store.receipt(request)
            previous = await self.store.reconciliation(result.id, result.scope)
            if previous is not None:
                if previous != fingerprint:
                    raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return job
            if job.status != OperationState.OUTCOME_UNKNOWN:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            status = OperationState(result.status)
            updated = self._edit(job, status=status)
            attempt = Attempt(lease, ATTEMPT_STATUS[result.status], self.clock.utc())
            await self._save(
                updated,
                schedule,
                request,
                previous=job,
                attempt=attempt,
                reconciliation=result,
            )
            self._ack_resource(result)
            self._leases.pop(lease.attempt_id, None)
            self._deadlines.pop(job.operation_id, None)
            return updated

    async def reconcile(self) -> int:
        """Bounded pass. Restart has no local permits/deadlines: old jobs close."""
        async with self._lock:
            changed = 0
            page = await self.store.active(
                limit=self.limits.batch,
                cursor=self._reconcile_cursor,
            )
            self._reconcile_cursor = page.next_cursor
            for job, schedule in page.items:
                status: OperationState | None = None
                attempt: Attempt | None = None
                try:
                    self._deadline(job)
                    command = await self.store.command(job.command_id, job.scope)
                    self._admission(command)
                except ContractError:
                    status = (
                        OperationState.OUTCOME_UNKNOWN
                        if job.status == OperationState.RUNNING
                        else OperationState.CANCELLED
                    )
                if status is None and job.status == OperationState.RUNNING and job.lease:
                    if (
                        self.clock.utc() >= job.lease.expires_at
                        or self.clock.elapsed() >= self._leases.get(job.lease.attempt_id, -math.inf)
                    ):
                        status = OperationState.OUTCOME_UNKNOWN
                        if schedule.retry_safe and schedule.attempts < self.limits.attempts:
                            status = OperationState.WAITING
                if status is None:
                    continue
                if job.lease:
                    attempt = Attempt(
                        job.lease, "unknown", self.clock.utc(), ErrorCode.OUTCOME_UNKNOWN
                    )
                values: dict[str, object] = {"status": status}
                if status == OperationState.WAITING:
                    values.update(
                        waiting_reason="lease expired; safe retry", wake_event="retry.timer"
                    )
                    schedule = replace(
                        schedule,
                        ready_at=self.clock.utc()
                        + timedelta(
                            seconds=self.limits.retry_seconds * min(1.25, max(0.75, self.jitter()))
                        ),
                    )
                updated = self._edit(job, **values)
                await self._save(
                    updated,
                    schedule,
                    self._command(job, "reconcile-" + uuid4().hex),
                    previous=job,
                    attempt=attempt,
                )
                # No fabricated cancellation receipt for a vanished worker.
                if status in TERMINAL:
                    self._deadlines.pop(job.operation_id, None)
                    if job.lease:
                        self._leases.pop(job.lease.attempt_id, None)
                changed += 1
            return changed


class OutboxDispatcher:
    def __init__(
        self,
        store: JobStore,
        broker: Broker,
        *,
        limits: JobLimits | None = None,
        clock: Clock | None = None,
        jitter: Callable[[], float] = lambda: random.uniform(0.75, 1.25),
    ) -> None:
        self.store, self.broker = store, broker
        self.jitter = jitter
        self.limits, self.clock = limits or JobLimits(), clock or Clock()

    async def dispatch(self) -> int:
        delivered = 0
        for reference in await self.store.pending_events(limit=self.limits.batch):
            retry_at = self.clock.utc() + timedelta(
                seconds=self.limits.dispatch_seconds * min(1.25, max(0.75, self.jitter()))
            )
            try:
                await self.broker.publish(reference)
            except ContractError:
                await self.store.delivery_failed(reference, retry_at=retry_at)
                continue
            await self.store.dispatched(reference, retry_at=retry_at)
            delivered += 1
        return delivered
