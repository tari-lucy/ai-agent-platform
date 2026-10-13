"""Single-event-loop state owner. Revocation performs bounded in-memory work only."""

import asyncio
import inspect
import math
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, get_ident
from typing import Literal
from uuid import uuid4

from ai_agent_contracts.api import ControlCommand, Receipt, StopCommand, StopReceipt
from ai_agent_contracts.codec import ContractError, check_version, command_fingerprint
from ai_agent_contracts.common import Id, Scope
from ai_agent_contracts.control import Freshness, RunSession
from ai_agent_contracts.states import ActivityState, ErrorCode, RunState
from pydantic import TypeAdapter, ValidationError

from .ports import (
    Admission,
    Clock,
    ContextApproval,
    ControlAuthority,
    ControlLimits,
    Readiness,
    ResourceTicket,
    RunJournal,
    WorkKind,
)


@dataclass
class _Resource:
    ticket: ResourceTicket
    admission: Admission


class Supervisor:
    def __init__(
        self,
        scope: Scope,
        agent_id: str,
        journal: RunJournal,
        readiness: Readiness,
        *,
        clock: Clock | None = None,
        limits: ControlLimits | None = None,
    ) -> None:
        if scope.agent_id is not None or scope.run_id is not None or scope.session_id is not None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self.clock, self.limits = clock or Clock(), limits or ControlLimits()
        self.journal, self.readiness = journal, readiness
        self._thread = get_ident()
        self._base, self._agent = scope, agent_id
        self._run = self._new_run(RunState.STOPPED, "observation")
        self._approval: ContextApproval | None = None
        self._admissions: dict[str, Admission] = {}
        self._resources: dict[str, _Resource] = {}
        self._requests: dict[tuple[str, str], tuple[str, Receipt]] = {}
        self._write_lock = asyncio.Lock()
        self._recovered = False
        self._recovering = False
        self._transitioning = False
        self._dirty = False
        self._deadline: float | None = None
        self._pause_deadline: float | None = None
        self._last_elapsed, self._last_utc = self.clock.elapsed(), self.clock.utc()
        self.last_error: ErrorCode | None = None

    def _owned(self) -> None:
        if get_ident() != self._thread:
            raise RuntimeError("CONTROL_OWNER_THREAD_REQUIRED")

    def _new_run(
        self, state: RunState, autonomy: Literal["observation", "autonomous"]
    ) -> RunSession:
        run_id, session_id = uuid4().hex, uuid4().hex
        scope = self._base.model_copy(
            update={"agent_id": self._agent, "run_id": run_id, "session_id": session_id}
        )
        now = self.clock.utc()
        return RunSession(
            id=run_id,
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            agent_id=self._agent,
            run_id=run_id,
            session_id=session_id,
            state=state,
            autonomy=autonomy,
            activity=ActivityState.OPEN,
            voice_enabled=False,
            gm_paused=True,
            control_epoch=0,
        )

    @property
    def snapshot(self) -> RunSession:
        self.poll()
        return self._run.model_copy(deep=True)

    @property
    def persistence_pending(self) -> bool:
        return self._dirty

    def _change(self, **changes: object) -> None:
        now = max(self.clock.utc(), self._run.updated_at)
        values = self._run.model_dump()
        values.update(changes, version=self._run.version + 1, updated_at=now)
        self._run = RunSession.model_validate(values)
        self._dirty = True

    def _cancel(self, kinds: frozenset[WorkKind] | None = None) -> None:
        for admission_id, admission in list(self._admissions.items()):
            if kinds is None or admission.kind in kinds:
                admission.cancelled.set()
                del self._admissions[admission_id]

    def _revoke(self) -> None:
        self._cancel()
        self._approval = None
        self._change(control_epoch=self._run.control_epoch + 1)
        self._pause_deadline = None

    def _pending(self, kinds: frozenset[WorkKind] | None = None) -> list[str]:
        return sorted(
            {
                resource.ticket.resource_id
                for resource in self._resources.values()
                if kinds is None or resource.admission.kind in kinds
            }
        )

    def _settle(self) -> None:
        pending = self._pending()
        if self._run.state == RunState.STOPPING and not pending:
            self._change(
                state=RunState.STOPPED, ended_at=self.clock.utc(), unconfirmed_resource_ids=[]
            )
            self._deadline = None
        elif self._run.activity == ActivityState.PAUSING and not self._pending(
            frozenset({"activity", "voice", "gm", "reflection"})
        ):
            self._change(activity=ActivityState.PAUSED, unconfirmed_resource_ids=pending)
            self._pause_deadline = None

    def poll(self) -> None:
        self._owned()
        elapsed, utc = self.clock.elapsed(), self.clock.utc()
        gap = elapsed - self._last_elapsed
        wall_gap = (utc - self._last_utc).total_seconds()
        self._last_elapsed, self._last_utc = elapsed, utc
        if self._run.state in {RunState.STARTING, RunState.ACTIVE} and (
            gap > self.limits.sleep_gap_seconds
            or abs(wall_gap - gap) > self.limits.sleep_gap_seconds
            or gap < 0
        ):
            self.critical_failure(ErrorCode.STALE_CONTEXT)
        for admission_id, admission in list(self._admissions.items()):
            if elapsed >= admission.expires_at:
                admission.cancelled.set()
                del self._admissions[admission_id]
        if self._approval is not None and elapsed >= self._approval.expires_at:
            self.critical_failure(ErrorCode.EXPIRED)
        self._settle()
        if self._deadline is not None and elapsed >= self._deadline:
            self._change(state=RunState.FAULTED, unconfirmed_resource_ids=self._pending())
            self.last_error = ErrorCode.OUTCOME_UNKNOWN
            self._deadline = None
        if self._pause_deadline is not None and elapsed >= self._pause_deadline:
            self.critical_failure(ErrorCode.OUTCOME_UNKNOWN)
            self._change(activity=ActivityState.PAUSED)
            self._pause_deadline = None

    async def watchdog(self) -> None:
        """Only a local timer; no journal, Redis, model or arbitrary adapter callbacks."""
        while True:
            self.poll()
            await asyncio.sleep(0.05)

    def critical_failure(self, code: ErrorCode = ErrorCode.SERVICE_UNAVAILABLE) -> None:
        self._owned()
        self._revoke()
        self._change(state=RunState.FAULTED, unconfirmed_resource_ids=self._pending())
        self.last_error = code
        self._deadline = None

    def shutdown(self) -> None:
        """Process lifecycle hook; never waits for storage or executor acknowledgment."""
        self._owned()
        self._revoke()
        state = RunState.FAULTED if self._resources else RunState.STOPPED
        self._change(state=state, unconfirmed_resource_ids=self._pending())

    def _authority(self, authority: ControlAuthority, scope: Scope, target: str) -> None:
        self.poll()
        if (
            self.clock.elapsed() >= authority.expires_at
            or (authority.is_current is not None and authority.is_current() is not True)
            or scope.partition not in authority.partitions
            or scope.partition != self._base.partition
            or target != self._agent
            or scope.agent_id != self._agent
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def _current_scope(self, scope: Scope) -> None:
        if (scope.run_id is not None and scope.run_id != self._run.run_id) or (
            scope.session_id is not None and scope.session_id != self._run.session_id
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)

    def _replay(
        self, authority: ControlAuthority, command: ControlCommand | StopCommand
    ) -> Receipt | None:
        self._authority(authority, command.scope, command.target)
        saved = self._requests.get((authority.principal_id, command.request_id))
        if saved is not None:
            if saved[0] != command_fingerprint(command):
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT, command.request_id)
            return saved[1].model_copy(deep=True)
        self._current_scope(command.scope)
        return None

    def _remember(
        self, authority: ControlAuthority, command: ControlCommand | StopCommand, receipt: Receipt
    ) -> None:
        key = (authority.principal_id, command.request_id)
        # STOP never waits or fails because a deduplication buffer is full.
        if key in self._requests or len(self._requests) < self.limits.requests:
            self._requests[key] = (command_fingerprint(command), receipt.model_copy(deep=True))

    def _receipt(
        self,
        command: ControlCommand,
        status: Literal["accepted", "completed", "waiting", "rejected"] = "completed",
    ) -> Receipt:
        return Receipt(
            request_id=command.request_id,
            scope=command.scope,
            status=status,
            resource_id=self._run.id,
            version=self._run.version,
        )

    def _stop_receipt(self, command: StopCommand) -> StopReceipt:
        return StopReceipt(
            request_id=command.request_id,
            scope=command.scope,
            status="completed" if self._run.state == RunState.STOPPED else "waiting",
            resource_id=self._run.id,
            version=self._run.version,
            state=self._run.state,
            control_epoch=self._run.control_epoch,
            run_id=self._run.run_id,
            unconfirmed_resource_ids=self._pending(),
        )

    def stop(self, authority: ControlAuthority, command: StopCommand) -> StopReceipt:
        """Direct cancellation: no lock, coroutine, disk, broker or model in this path."""
        replay = self._replay(authority, command)
        if replay is not None:
            if not isinstance(replay, StopReceipt):
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT, command.request_id)
            if replay.run_id == self._run.run_id and self._run.state in {
                RunState.STOPPING,
                RunState.STOPPED,
                RunState.FAULTED,
            }:
                return self._stop_receipt(command)
            return replay
        self._revoke()
        self._change(state=RunState.STOPPING, unconfirmed_resource_ids=self._pending())
        self._deadline = self.clock.elapsed() + self.limits.stop_seconds
        self._settle()
        receipt = self._stop_receipt(command)
        self._remember(authority, command, receipt)
        return receipt

    async def recover(self) -> None:
        """Fresh boot remains closed; old snapshots cannot authorize a new run."""
        self._owned()
        if self._recovering or self._transitioning or self._run.state != RunState.STOPPED:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        self._recovering, self._recovered = True, False
        try:
            previous_runs = await self.journal.load()
            unresolved = False
            for previous in previous_runs:
                if previous.scope.workspace_id != self._base.workspace_id:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if previous.state == RunState.STOPPED:
                    continue
                unknown = await self.readiness.reconcile(previous)
                # Reconciliation is allowed to close records, never to resurrect admission.
                values = previous.model_dump()
                values.update(
                    state=RunState.FAULTED if unknown else RunState.STOPPED,
                    control_epoch=previous.control_epoch + 1,
                    unconfirmed_resource_ids=list(unknown),
                    ended_at=max(self.clock.utc(), previous.updated_at),
                    updated_at=max(self.clock.utc(), previous.updated_at),
                )
                await self.journal.save(RunSession.model_validate(values))
                unresolved = unresolved or bool(unknown)
            if unresolved:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            self._recovered = True
            self.last_error = None
        except ContractError as error:
            self.last_error = error.error.code
            raise
        except Exception:
            self.last_error = ErrorCode.SERVICE_UNAVAILABLE
            raise
        finally:
            self._recovering = False

    async def _persist(self, snapshot: RunSession) -> None:
        async with self._write_lock:
            await self.journal.save(snapshot)

    async def flush(self) -> None:
        """Explicit bounded retry by the coordinator/scheduler, not an infinite IO loop."""
        self._owned()
        try:
            snapshot = self._run.model_copy(deep=True)
            await self._persist(snapshot)
            self._dirty = self._run != snapshot
            if not self._dirty and self._run.state != RunState.FAULTED:
                self.last_error = None
        except Exception:
            self._dirty = True
            self.last_error = ErrorCode.SERVICE_UNAVAILABLE
            # Losing durable health closes active execution; STOP remains independent.
            if self._run.state in {RunState.STARTING, RunState.ACTIVE}:
                self.critical_failure()
            raise

    def _approval_valid(self, approval: ContextApproval, run: RunSession) -> None:
        if (
            approval.scope != run.scope
            or type(approval.profile_version) is not int
            or approval.profile_version < 1
            or type(approval.policy_version) is not int
            or approval.policy_version < 1
            or not math.isfinite(approval.expires_at)
            or not self.clock.elapsed() < approval.expires_at
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)

    async def control(self, authority: ControlAuthority, command: ControlCommand) -> Receipt:
        replay = self._replay(authority, command)
        if replay is not None:
            return replay
        if len(self._requests) >= self.limits.requests:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        check_version(command.expected_version, self._run.version, command.request_id)
        if command.type == "session.end":
            self._revoke()
            self._change(state=RunState.STOPPING, unconfirmed_resource_ids=self._pending())
            self._deadline = self.clock.elapsed() + self.limits.stop_seconds
            self._settle()
            receipt = self._receipt(command, "waiting" if self._resources else "completed")
            self._remember(authority, command, receipt)
            return receipt
        if command.type == "agent.pause":
            return self._pause(authority, command)
        if self._transitioning or self._recovering:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if not self._recovered:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        is_start = command.type == "agent.start"
        if is_start:
            if self._run.state != RunState.STOPPED or self._resources or self._dirty:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            self._run = self._new_run(
                RunState.STARTING, command.arguments.autonomy or "observation"
            )
            self._dirty = True
        elif (
            self._run.state != RunState.ACTIVE
            or self._run.activity != ActivityState.PAUSED
            or self._pending(frozenset({"activity", "voice", "gm", "reflection"}))
        ):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        token = (self._run.run_id, self._run.control_epoch, self._run.version)
        self._transitioning = True
        self._remember(authority, command, self._receipt(command, "accepted"))
        try:
            if is_start:
                await self._persist(self._run.model_copy(deep=True))
            self.poll()
            self._authority(authority, command.scope, command.target)
            if token != (self._run.run_id, self._run.control_epoch, self._run.version):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            approval = await self.readiness.check(self._run.model_copy(deep=True))
            self.poll()
            self._authority(authority, command.scope, command.target)
            if token != (self._run.run_id, self._run.control_epoch, self._run.version):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            self._approval_valid(approval, self._run)
            values = self._run.model_dump()
            values.update(
                state=RunState.ACTIVE,
                activity=ActivityState.OPEN,
                autonomy=command.arguments.autonomy or self._run.autonomy,
                version=self._run.version + 1,
                updated_at=max(self.clock.utc(), self._run.updated_at),
                started_at=self._run.started_at or self.clock.utc(),
            )
            candidate = RunSession.model_validate(values)
            # Durable reservation BEFORE opening memory admission, including workspace uniqueness.
            await self._persist(candidate)
            self.poll()
            self._authority(authority, command.scope, command.target)
            self._approval_valid(approval, candidate)
            if token != (self._run.run_id, self._run.control_epoch, self._run.version):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            self._run, self._approval, self._dirty = candidate, approval, False
            self.last_error = None
            receipt = self._receipt(command)
            self._remember(authority, command, receipt)
            return receipt
        except BaseException:
            if token == (self._run.run_id, self._run.control_epoch, self._run.version):
                self.critical_failure()
            self._dirty = True
            self._remember(authority, command, self._receipt(command, "rejected"))
            raise
        finally:
            self._transitioning = False

    def _pause(self, authority: ControlAuthority, command: ControlCommand) -> Receipt:
        if self._run.state != RunState.ACTIVE or self._run.activity != ActivityState.OPEN:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        kinds: frozenset[WorkKind] = frozenset({"activity", "voice", "gm", "reflection"})
        self._cancel(kinds)
        # Observation/technical work stays independent. Old autonomous tokens never resume.
        self._change(activity=ActivityState.PAUSING, unconfirmed_resource_ids=self._pending())
        self._pause_deadline = self.clock.elapsed() + self.limits.pause_seconds
        self._settle()
        receipt = self._receipt(
            command, "waiting" if self._pause_deadline is not None else "completed"
        )
        self._remember(authority, command, receipt)
        return receipt

    def set_voice(
        self, authority: ControlAuthority, enabled: bool, *, expected_version: int
    ) -> None:
        self._authority(authority, self._run.scope, self._agent)
        check_version(expected_version, self._run.version)
        if type(enabled) is not bool or self._run.state != RunState.ACTIVE:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if enabled and self._pending(frozenset({"voice"})):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if not enabled:
            self._cancel(frozenset({"voice"}))
        self._change(voice_enabled=enabled)

    def set_gm(self, authority: ControlAuthority, paused: bool, *, expected_version: int) -> None:
        self._authority(authority, self._run.scope, self._agent)
        check_version(expected_version, self._run.version)
        if type(paused) is not bool or self._run.state != RunState.ACTIVE:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if not paused and self._pending(frozenset({"gm"})):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if paused:
            self._cancel(frozenset({"gm"}))
        self._change(gm_paused=paused)

    def _eligible(self, kind: WorkKind) -> bool:
        if self._run.state != RunState.ACTIVE or self._approval is None or not self._recovered:
            return False
        if kind == "observation":
            return True
        return (
            self._run.autonomy == "autonomous"
            and self._run.activity == ActivityState.OPEN
            and (kind != "voice" or self._run.voice_enabled)
            and (kind != "gm" or not self._run.gm_paused)
        )

    def admit(self, kind: WorkKind, *, seconds: float | None = None) -> Admission:
        self.poll()
        if kind not in {"activity", "voice", "gm", "reflection", "observation"}:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if not self._eligible(kind):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if len(self._admissions) >= self.limits.admissions:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        duration = self.limits.lease_seconds if seconds is None else seconds
        if not 0 < duration <= self.limits.lease_seconds:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        assert self._approval is not None
        admission = Admission(
            id=uuid4().hex,
            scope=self._run.scope,
            freshness=Freshness(
                run_id=self._run.run_id,
                control_epoch=self._run.control_epoch,
                profile_version=self._approval.profile_version,
                policy_version=self._approval.policy_version,
            ),
            kind=kind,
            expires_at=min(self.clock.elapsed() + duration, self._approval.expires_at),
            cancelled=Event(),
        )
        self._admissions[admission.id] = admission
        return admission

    def validate(self, admission: Admission) -> None:
        self.poll()
        if (
            self._admissions.get(admission.id) is not admission
            or admission.cancelled.is_set()
            or admission.scope != self._run.scope
            or admission.freshness.run_id != self._run.run_id
            or admission.freshness.control_epoch != self._run.control_epoch
            or self._approval is None
            or admission.freshness.profile_version != self._approval.profile_version
            or admission.freshness.policy_version != self._approval.policy_version
            or not self._eligible(admission.kind)
            or self.clock.elapsed() >= admission.expires_at
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)

    def resolve(self, admission_id: str, *, scope: Scope, freshness: Freshness) -> Admission:
        """Job owner uses stored ExecutionCommand.cancellation_id, not a new permit.

        Matching epoch alone is insufficient after resource-scoped pause/mute.
        The old ID remains invalid after resume, even within the same run/epoch.
        """
        self.poll()
        admission = self._admissions.get(admission_id)
        if admission is None or admission.scope != scope or admission.freshness != freshness:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.validate(admission)
        return admission


    async def refresh(self, admission: Admission, *, seconds: float | None = None) -> Admission:
        """Renew the SAME cancellation identity after current readiness checks.

        Never revives an expired, paused or stopped admission. The old snapshot
        becomes invalid; resources share its cancellation Event. Worker fencing
        and per-action rights remain the job owner's responsibility.
        """
        self.validate(admission)
        duration = self.limits.lease_seconds if seconds is None else seconds
        if not math.isfinite(duration) or not 0 < duration <= self.limits.lease_seconds:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        approval = await self.readiness.check(self.snapshot)
        self.validate(admission)
        self._approval_valid(approval, self._run)
        if (approval.profile_version, approval.policy_version) != (
            admission.freshness.profile_version, admission.freshness.policy_version
        ):
            self.critical_failure(ErrorCode.STALE_CONTEXT)
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self._approval = approval
        renewed = Admission(
            admission.id, admission.scope, admission.freshness, admission.kind,
            min(self.clock.elapsed() + duration, approval.expires_at), admission.cancelled,
        )
        self._admissions[admission.id] = renewed
        return renewed

    def finish[T](self, admission: Admission, apply: Callable[[], T]) -> T:
        """Validate at the final local owner handoff, with NO await between check and apply.

        Only bounded nonblocking synchronous owner mutation/handoff belongs here.
        Provider work/DB transactions finish BEFORE this call. Async callbacks
        are rejected. This is not an atomic transaction with an external API.
        """
        if inspect.iscoroutinefunction(apply):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self.validate(admission)
        result = apply()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return result

    def register(self, admission: Admission, resource_id: str) -> ResourceTicket:
        self.validate(admission)
        try:
            TypeAdapter(Id).validate_python(resource_id)
        except ValidationError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        if len(self._resources) >= self.limits.resources or any(
            item.ticket.resource_id == resource_id for item in self._resources.values()
        ):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        ticket = ResourceTicket(uuid4().hex, resource_id, admission.id, admission.scope)
        self._resources[ticket.id] = _Resource(ticket, admission)
        self._change(unconfirmed_resource_ids=self._pending())
        return ticket

    def acknowledge(self, ticket: ResourceTicket) -> None:
        """Only a real executor completion/cancel receipt; lease expiry is NOT a receipt."""
        self.poll()
        resource = self._resources.get(ticket.id)
        if resource is None or resource.ticket != ticket:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        del self._resources[ticket.id]
        self._change(unconfirmed_resource_ids=self._pending())
        self._settle()

    def retire(self, admission: Admission) -> None:
        self._owned()
        if self._admissions.get(admission.id) is admission:
            admission.cancelled.set()
            del self._admissions[admission.id]
