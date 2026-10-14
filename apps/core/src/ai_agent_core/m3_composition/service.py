"""One internal executor over the accepted JobOwner, Supervisor and ResourceScheduler."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, suppress
from dataclasses import replace
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from ai_agent_adapters.control.journal import SQLiteRunJournal
from ai_agent_adapters.jobs.store import SQLiteJobs
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import Admission, ControlAuthority
from ai_agent_application.development import DevelopmentController, MemoryEvidence, Reflector
from ai_agent_application.dialogue import ControlSnapshot
from ai_agent_application.extraction import Consolidator, Extractor
from ai_agent_application.gamemaster import Classification, Dice, Family, GameMaster
from ai_agent_application.gamemaster.clock import ActiveGMClock
from ai_agent_application.gamemaster.rules import StoryRule
from ai_agent_application.jobs.owner import TERMINAL, JobOwner
from ai_agent_application.jobs.ports import WorkerAuthority
from ai_agent_application.laboratory import LabRunner, VirtualClock
from ai_agent_application.laboratory.catalogue import catalogue
from ai_agent_application.llm import (
    AttemptReport,
    ChatMessage,
    Failure,
    Generation,
    GenerationRequest,
    LLMGateway,
    ProviderFactory,
)
from ai_agent_application.memory import MemoryService
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.memory.service import derived_id
from ai_agent_application.scheduling import Demand, Lane, ResourceScheduler
from ai_agent_application.scheduling.model import BudgetLease
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_application.telemetry import Outcome
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.api import (
    ServiceClaimCommand,
    ServiceLeaseCommand,
    ServiceResultCommand,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import ExecutionCommand, ExecutionResult, Job, Lease
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import Connection
from ai_agent_contracts.memory import Episode, Source
from ai_agent_contracts.persona import StateChange
from ai_agent_contracts.states import ChangeState, ErrorCode, OperationState, RunState
from ai_agent_contracts.workspace import Capability

from ai_agent_core.access import AccessPorts

from .artifacts import Reports
from .policy import WORKER_ID, Policy, Readiness
from .schemas import JobArguments, StoryVerdict, TaskInput, TaskResult
from .state import CAPABILITIES, PRINCIPALS, ROLES, State, command, extension, put, record, rules_of

DOMAIN_OWNERS = frozenset(
    {
        Owner.MEMORY,
        Owner.PERSONA,
        Owner.GM,
        Owner.LABORATORY,
        Owner.CATALOGUE,
        Owner.SOCIAL,
        Owner.GATEWAY,
        Owner.CHANNEL,
    }
)


class Runtime:
    def __init__(
        self,
        store: SQLiteStore,
        registry: ExtensionRegistry,
        supervisor: Supervisor,
        scheduler: ResourceScheduler,
        providers: ProviderFactory,
        *,
        scopes: tuple[Scope, ...],
        allowed_adapters: frozenset[str] = frozenset({"mock"}),
        families: tuple[Family, ...] = (),
        lab: LabRunner | None = None,
        reports: Reports | None = None,
        reader_access: tuple[RecordAccess, ...] = (),
        dialogue_index: RecallIndex | None = None,
    ) -> None:
        if scheduler.supervisor is not supervisor or store.extensions is not registry:
            raise ValueError("M3_COMPOSITION")
        if (
            not scopes
            or any(s.agent_id is not None for s in scopes)
            or supervisor.snapshot.scope.partition not in {s.partition for s in scopes}
        ):
            raise ValueError("M3_SCOPES")
        self.store, self.registry, self.supervisor, self.scheduler = (
            store,
            registry,
            supervisor,
            scheduler,
        )
        self.providers, self.scopes, self.families, self.lab = providers, scopes, families, lab
        self.readers = reader_access
        self.dialogue_index = dialogue_index
        from ai_agent_core.access.events import Events
        from ai_agent_core.access.sessions import AccessLimits

        self.events = Events(AccessLimits())
        self.reports = reports
        if lab is not None and reports is not None:
            if lab.storage is not store or reports.storage is not store:
                raise ValueError("M3_REPORT_STORE")
            lab.artifact_writer = reports.save
        self.state = State(store, registry)
        self.policy = Policy(store, self.state, supervisor, allowed_adapters=allowed_adapters)
        self.jobs = JobOwner(
            SQLiteJobs(store, PRINCIPALS[Owner.JOBS], reader_access=reader_access),
            supervisor,
            self.policy,
            extensions=registry,
        )
        supervisor.journal = SQLiteRunJournal(store, PRINCIPALS[Owner.SUPERVISOR], scopes)
        supervisor.readiness = Readiness(self.policy)
        self.worker = WorkerAuthority(
            WORKER_ID,
            frozenset(s.partition for s in scopes),
            frozenset(CAPABILITIES),
            supervisor.clock.elapsed() + 86400,
        )
        self.clock: ActiveGMClock | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._heartbeat: Admission | None = None
        self._active: asyncio.Task[Job] | None = None
        self._lock = asyncio.Lock()
        self._checkpoint_at = self._refresh_at = self._reconcile_at = 0.0
        self._opened = False
        from .lab_control import LabControl

        self.lab_control = LabControl(self)

    def notify(self, kind: str, scope: Scope, identifier: str, version: int | None = None) -> None:
        from ai_agent_contracts.api import PanelEvent

        now = self.supervisor.clock.utc()
        event_id = uuid4().hex
        self.events.publish(
            PanelEvent.model_validate(
                dict(
                    id=event_id,
                    event_id=event_id,
                    type=kind,
                    scope=scope,
                    created_at=now,
                    occurred_at=now,
                    resource_id=identifier,
                    resource_version=version,
                    causal_id=None,
                    operation_id=None,
                )
            )
        )

    def authorize_configuration(self, authority: ControlAuthority, scope: Scope) -> None:
        # Owner configuration may use a partition-only scope. Control/messages still require
        # their explicit current run. An explicit foreign agent is never rewritten.
        current = self.supervisor.snapshot
        if scope.agent_id not in (None, current.agent_id):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        checked = scope.model_copy(update={"agent_id": current.agent_id})
        self.supervisor.authorize_operator(authority, checked)

    def access_ports(self) -> AccessPorts:
        from ai_agent_core.access import AccessPorts
        from ai_agent_core.access.reads import SQLiteReader

        from .access import Mutations
        from .downloads import Downloads
        from .views import Views

        return AccessPorts(
            reader=SQLiteReader(self.store),
            views=Views(self),
            downloads=Downloads(self),
            mutations=Mutations(self),
            jobs=self.jobs,
            events=self.events,
            extensions=self.registry,
            scopes=self.scopes,
        )

    async def open(self) -> None:
        await self.store.check_schema()  # no implicit migrations/provisioning
        # A bounded full pass closes prior queued/running jobs; it cannot revive old permits.
        for _ in range(6):
            await self.jobs.reconcile()
        await self._recover_usage()
        try:
            await self.supervisor.recover()
        except ContractError as error:
            if error.error.code != ErrorCode.OUTCOME_UNKNOWN:
                raise
            # Keep authenticated STOP/read access alive. Start remains closed until
            # the original resource owner confirms reconciliation.
        gm = await self.state.gm(self.supervisor.snapshot.scope)
        self.clock = ActiveGMClock(
            self.supervisor.clock.elapsed,
            initial=gm.active_seconds,
            max_gap=self.supervisor.limits.sleep_gap_seconds,
        )
        self._unsubscribe = self.supervisor.observe(self.clock.observe)
        self._opened = True

    async def _recover_usage(self) -> None:
        from ai_agent_contracts.llm import LLMAttempt

        for scope in self.scopes:
            repo = self.store.repository(LLMAttempt, PRINCIPALS[Owner.LLM], scope)
            after = None
            for _ in range(50):
                page = await repo.list(limit=200, after_id=after)
                for row in page:
                    if row.status != "running":
                        continue
                    updated = row.model_copy(
                        update={
                            "version": row.version + 1,
                            "status": "unknown",
                            "updated_at": max(self.supervisor.clock.utc(), row.updated_at),
                        }
                    )
                    await put(
                        self.store,
                        self.registry,
                        Owner.LLM,
                        row.scope,
                        f"recover-attempt-{row.id}-{updated.version}",
                        updated,
                        row.version,
                    )
                if len(page) < 200:
                    break
                after = page[-1].id
            else:
                raise ContractError(ErrorCode.RESOURCE_BUSY)

    def active_seconds(self) -> float:
        self.supervisor.poll()
        if self.clock is None:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        return self.clock.sample()

    async def close(self) -> None:
        self._opened = False
        self.supervisor.shutdown()
        if self._active is not None:
            self._active.cancel()
            with suppress(asyncio.CancelledError, ContractError):
                await self._active
        if self.clock is not None:
            await self.state.checkpoint_time(self.supervisor.snapshot.scope, self.active_seconds())
        await self.supervisor.flush()
        if self._unsubscribe:
            self._unsubscribe()

    async def tick(self) -> None:
        if not self._opened:
            return
        self.supervisor.poll()
        now = self.supervisor.clock.elapsed()
        if self.supervisor.persistence_pending:
            await self.supervisor.flush()
        run = self.supervisor.snapshot
        if now >= self._checkpoint_at:
            await self.state.checkpoint_time(run.scope, self.active_seconds())
            if self.reports is not None:
                self.reports.sweep()
            self._checkpoint_at = now + 5
        if run.state == RunState.ACTIVE and now >= self._refresh_at:
            try:
                if self._heartbeat is None or self._heartbeat.cancelled.is_set():
                    self._heartbeat = self.supervisor.admit("observation")
                self._heartbeat = await self.supervisor.refresh(self._heartbeat)
            except ContractError:
                self._heartbeat = None
                self.supervisor.critical_failure(ErrorCode.STALE_CONTEXT)
                raise
            self._refresh_at = now + 5
        if now >= self._reconcile_at:
            await self.jobs.reconcile()
            self._reconcile_at = now + 5
        if self._active is not None:
            if not self._active.done():
                return
            self._active.result()
            self._active = None
        page = await self.jobs.store.active(limit=50)
        for job, schedule in page.items:
            if (
                job.scope == self.supervisor.snapshot.scope
                and job.status in (OperationState.QUEUED, OperationState.WAITING)
                and schedule.ready_at <= self.supervisor.clock.utc()
            ):
                self._active = asyncio.create_task(self.execute(job))
                break

    async def submit(self, authority: ControlAuthority, request_id: str, request: TaskInput) -> Job:
        if not self._opened:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        async with self._lock:
            scope = self.supervisor.snapshot.scope
            self.supervisor.authorize_operator(authority, scope)
            identifier = derived_id("m3cmd", scope, request_id)
            existing = await self.store.repository(
                ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope
            ).get(identifier)
            if existing is not None:
                if existing.scope != scope:
                    raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
                args = self.registry.validate(
                    existing.arguments.schema_id,
                    existing.arguments.schema_version,
                    existing.arguments.data,
                )
                if not isinstance(args, JobArguments) or args.request != request:
                    raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return await self.jobs.submit(
                    existing.id, existing.scope, goal="M3 " + request.kind
                )
            if request.kind == "dialogue" and self.dialogue_index is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            if request.kind == "laboratory":
                if (
                    request.lab_scope is None
                    or request.lab_scope.partition not in {s.partition for s in self.scopes}
                    or request.lab_scope.partition not in authority.partitions
                ):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                from ai_agent_application.laboratory.lab import PERSONA as LAB_PERSONA
                from ai_agent_contracts.persona import Profile

                used = await self.store.repository(Profile, LAB_PERSONA, request.lab_scope).list(
                    limit=1
                )
                if used:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                pending = await self.jobs.store.active(limit=200)
                if pending.next_cursor is not None:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                for other, _ in pending.items:
                    if other.capability_id != "m3.laboratory":
                        continue
                    other_command = await self.jobs.store.command(other.command_id, other.scope)
                    other_args = self.registry.validate(
                        other_command.arguments.schema_id,
                        other_command.arguments.schema_version,
                        other_command.arguments.data,
                    )
                    if (
                        isinstance(other_args, JobArguments)
                        and other_args.request.lab_scope == request.lab_scope
                    ):
                        raise ContractError(ErrorCode.RESOURCE_BUSY)
            capability, permission, _ = await self.policy.permit(scope, request.kind)
            context = await self.policy.context(scope)
            if permission.policy_version != context.workspace.policy_version:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            roles = await self.state.roles(scope)
            selected = [roles[r] for r in ROLES[request.kind] if r in roles]
            if len(selected) != len(ROLES[request.kind]):
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            connections = {}
            for row in selected:
                resolved = await self.state.binding(scope, row)
                if (
                    resolved.adapter_id not in self.policy.allowed_adapters
                    or row.fallback_assignment_id is not None
                ):
                    raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
                connection = await self.store.repository(
                    Connection, PRINCIPALS[Owner.WORKSPACE], scope
                ).get(row.connection_id)
                assert connection is not None
                connections[connection.id] = connection.version
            refs: dict[str, SourceRef] = {}
            for episode_id in request.episode_ids:
                episode = await self.store.repository(Episode, PRINCIPALS[Owner.MEMORY], scope).get(
                    episode_id, purpose="api_transfer"
                )
                if episode is None:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                refs.update((ref.source_id, ref) for ref in episode.sources)
            for source_id in request.source_ids:
                source = await self.store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).get(
                    source_id, purpose="api_transfer"
                )
                if source is None:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                refs[source_id] = SourceRef(
                    source_id=source.id,
                    version=source.version,
                    scope=scope,
                    availability="available",
                )
            admission = self.supervisor.admit(
                "dialogue"
                if request.kind == "dialogue"
                else "gm"
                if request.kind == "gm"
                else "reflection"
            )
            now = self.supervisor.clock.utc()
            try:
                gm = await self.state.gm(scope)
                arguments = JobArguments(
                    request=request,
                    roles={r.id: r.version for r in selected},
                    connections=connections,
                    rules_version=context.rules_version,
                    simulation_start=now,
                    gm_intensity=gm.intensity,
                )
                execution = record(
                    self.registry,
                    ExecutionCommand,
                    id=identifier,
                    scope=scope,
                    created_at=now,
                    type="execution.command",
                    operation_id=derived_id("m3job", scope, request_id),
                    target=WORKER_ID,
                    binding_id=WORKER_ID,
                    capability_id=capability.id,
                    capability_version=capability.version,
                    arguments=extension(self.registry, "m3.job", arguments.model_dump(mode="json")),
                    permission_id=permission.id,
                    permission_version=permission.version,
                    deadline=now + timedelta(milliseconds=capability.timeout_ms),
                    conditions=extension(self.registry, "m3.empty", {}),
                    cancellation_id=admission.id,
                    freshness=admission.freshness,
                    sources=list(refs.values()),
                )
                await self.policy.check(execution, None)

                def guard() -> None:
                    self.supervisor.authorize_operator(authority, scope)
                    self.supervisor.validate(admission)

                with self.store.fence(guard, owner=Owner.BEHAVIOR):
                    await put(
                        self.store,
                        self.registry,
                        Owner.BEHAVIOR,
                        scope,
                        "submit-" + identifier,
                        execution,
                        None,
                        (RecordAccess(PRINCIPALS[Owner.JOBS].id),),
                    )
                return await self.jobs.submit(identifier, scope, goal="M3 " + request.kind)
            except BaseException:
                self.supervisor.retire(admission)
                raise

    async def execute(self, job: Job) -> Job:
        if job.operation_id in self.lab_control.tasks:
            stored = await self.jobs.store.load(job.operation_id, job.scope)
            return stored[0] if stored is not None else job
        task = asyncio.current_task()
        assert task is not None
        self.lab_control.tasks[job.operation_id] = task
        try:
            return await self._execute(job)
        finally:
            self.lab_control.tasks.pop(job.operation_id, None)
            await self.lab_control.lifecycle_for_job(job.operation_id)

    async def _execute(self, job: Job) -> Job:
        claim = await self.jobs.claim(
            self.worker,
            ServiceClaimCommand(
                request_id=uuid4().hex,
                type="job.claim",
                scope=job.scope,
                target=job.id,
                operation_id=job.operation_id,
                expected_version=job.version,
                freshness=job.freshness,
            ),
        )
        if claim is None or not claim.acquired:
            return job
        context = Execution(self, claim.command, claim.lease)
        renewal: asyncio.Task[None] | None = None
        allocation = None
        status: Literal["SUCCEEDED", "FAILED", "CANCELLED"] = "FAILED"
        error_code: ErrorCode | None = None
        identifiers: list[str] = []
        try:
            capability, _, _ = await self.policy.permit(job.scope, context.args.request.kind)
            self.scheduler.register(
                capability,
                tuple(Demand(i, 1) for i in capability.resource_ids),
                Lane.FOREGROUND if context.args.request.kind == "dialogue" else Lane.BACKGROUND,
            )
            request = self.scheduler.request(claim.command, attempt_id=claim.lease.attempt_id)
            acquired = self.scheduler.acquire(request)
            if acquired is None:
                self.scheduler.cancel_waiting(request)
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            allocation = acquired.lease
            context.budget = allocation
            renewal = asyncio.create_task(context.renew())
            await context.verify()
            with ExitStack() as stack:
                for owner in DOMAIN_OWNERS:
                    stack.enter_context(self.store.fence(context.guard, owner=owner))
                stack.enter_context(
                    self.store.authorize_writes(
                        lambda connection: self.policy.transaction_check(
                            context.with_sources(), connection
                        ),
                        owners=DOMAIN_OWNERS,
                    )
                )
                identifiers = await self._perform(context)
                await context.verify()
            status = "SUCCEEDED"
        except asyncio.CancelledError:
            status = "CANCELLED"
        except ContractError as error:
            error_code = error.error.code
            status = (
                "CANCELLED"
                if error.error.code
                in (ErrorCode.STALE_CONTEXT, ErrorCode.ACCESS_DENIED, ErrorCode.EXPIRED)
                else "FAILED"
            )
        except Exception:
            status = "FAILED"
        finally:
            context.cancel.set()
            if renewal:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal
            if allocation:
                self.scheduler.complete(
                    allocation, Outcome.SUCCEEDED if status == "SUCCEEDED" else Outcome.CANCELLED
                )
        result = record(
            self.registry,
            ExecutionResult,
            id=derived_id("m3result", job.scope, claim.lease.attempt_id),
            scope=job.scope,
            created_at=self.supervisor.clock.utc(),
            type="execution.result",
            command_id=claim.command.id,
            operation_id=job.operation_id,
            attempt_id=claim.lease.attempt_id,
            lease_version=claim.lease.lease_version,
            freshness=job.freshness,
            status=status,
            evidence=[],
            completed_at=self.supervisor.clock.utc(),
            result=extension(
                self.registry,
                "m3.result",
                TaskResult(
                    status="completed"
                    if status == "SUCCEEDED"
                    else "cancelled"
                    if status == "CANCELLED"
                    else "failed",
                    record_ids=identifiers,
                ).model_dump(mode="json"),
            ),
        )
        try:
            accepted = await self.jobs.accept(
                self.worker,
                ServiceResultCommand(
                    request_id=uuid4().hex,
                    type="job.result",
                    scope=job.scope,
                    target=job.id,
                    result=result,
                ),
                transient=error_code == ErrorCode.RESOURCE_BUSY,
                error=error_code,
            )
        except ContractError:
            stored = await self.jobs.store.load(job.operation_id, job.scope)
            if stored is None or stored[0].status != OperationState.OUTCOME_UNKNOWN:
                raise
            # This executor really ended. Reconcile metadata only; never reapply the payload.
            factual = result.model_copy(
                update={
                    "id": derived_id("m3settled", job.scope, claim.lease.attempt_id),
                    "result": None,
                }
            )
            accepted = await self.jobs.settle_unknown(
                self.worker,
                ServiceResultCommand(
                    request_id=uuid4().hex,
                    type="job.result",
                    scope=job.scope,
                    target=job.id,
                    result=factual,
                ),
            )
        self.notify("job.changed", accepted.scope, accepted.operation_id, accepted.version)
        if accepted.status not in TERMINAL:
            return accepted
        with suppress(ContractError):
            token = self.supervisor.resolve(
                claim.command.cancellation_id, scope=job.scope, freshness=job.freshness
            )
            self.supervisor.retire(token)
        return accepted

    async def _perform(self, execution: Execution) -> list[str]:
        request, scope = execution.args.request, execution.command.scope
        memory = MemoryService(
            self.store,
            PRINCIPALS[Owner.MEMORY],
            self.registry,
            clock=self.supervisor.clock.utc,
            reader_access=self.readers,
        )
        ids: list[str] = []
        if request.kind == "dialogue":
            from .dialogue import perform

            return await perform(execution)
        if request.kind in ("extract", "post_session"):
            report = await Extractor(memory, execution, admitted=execution.allowed).extract(
                scope, request.source_ids, cancel=execution.cancel
            )
            if report.status not in ("completed", "nothing"):
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            ids.extend(a.claim_id for a in report.accepted)
        if request.kind in ("consolidate", "post_session"):
            assert request.period_start is not None and request.period_end is not None
            summary = await Consolidator(
                memory, execution, admitted=execution.allowed, clock=self.supervisor.clock.utc
            ).summarise(
                scope,
                request.source_ids,
                request.period_start,
                request.period_end,
                execution.cancel,
            )
            if summary is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            ids.append(summary.id)
        gm = await self.state.gm(scope)
        classifier = Classifier(execution)
        if request.kind in ("reflect", "post_session"):

            def control() -> ControlSnapshot:
                run = self.supervisor.snapshot
                return ControlSnapshot(run.state, run.activity, run.autonomy, execution.allowed())

            controller = DevelopmentController(
                self.store,
                PRINCIPALS[Owner.PERSONA],
                self.registry,
                MemoryEvidence(memory),
                control,
                rules=rules_of(gm, self.registry),
                classifier=classifier,
                clock=self.supervisor.clock.utc,
                reader_access=self.readers,
            )
            reflection = await Reflector(
                controller, execution, clock=self.supervisor.clock.utc
            ).reflect(scope, request.trigger_id, request.episode_ids, execution.cancel)
            if reflection.status in ("too_soon", "not_lived"):
                return ids
            if reflection.status not in ("completed", "kept", "duplicate"):
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            # Recovery after checkpoint completion but before READY changes were applied.
            repo = self.store.repository(StateChange, PRINCIPALS[Owner.PERSONA], scope)
            after = None
            for _ in range(50):
                page = await repo.list(limit=200, after_id=after)
                for change in page:
                    if not change.proposal_id.startswith(request.trigger_id + ":"):
                        continue
                    if change.status == ChangeState.READY:
                        await execution.verify()
                        await controller.apply(scope, change.id)
                    ids.append(change.id)
                if len(page) < 200:
                    break
                after = page[-1].id
            else:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
        if request.kind == "gm":
            service = GameMaster(
                self.store,
                PRINCIPALS[Owner.GM],
                self.registry,
                memory,
                rules_of(gm, self.registry),
                self.families,
                Dice(request.seed),
                classifier=classifier,
                admitted=execution.allowed,
                clock=self.supervisor.clock.utc,
                reader_access=self.readers,
                active_clock=self.active_seconds,
                intensity=gm.intensity,
            )
            refs = execution.command.sources
            arc_id = request.arc_id
            if arc_id is None:
                arc = await service.open_arc(
                    scope, request.trigger_id, refs, request.direction, request.boundaries
                )
                if arc.arc is None:
                    return []
                arc_id = arc.arc.id
            registered = await self.store.repository(
                Capability, PRINCIPALS[Owner.WORKSPACE], scope
            ).list(limit=200)
            available = tuple(cap.id for cap in registered if cap.health == "ready")
            offer = await service.suggest(
                scope,
                arc_id,
                refs,
                available,
                execution,
                context=request.trigger_id,
            )
            if offer.opportunity:
                ids.append(offer.opportunity.id)
        if request.kind == "laboratory":
            if self.lab is None or self.lab.artifact_writer is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            # M5-04 can inject an evaluated provider. This M3 entry is mock-only.
            if (
                self.lab.providers is not None
                or self.lab.resolve is not None
                or self.lab.mock is not True
            ):
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            scenario = next((s for s in catalogue() if s.scenario_id == request.scenario_id), None)
            if scenario is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            await execution.verify()
            assert request.lab_scope is not None

            async def allocated(label: str) -> Scope:
                assert self.lab is not None and request.lab_scope is not None
                from ai_agent_contracts.workspace import LaboratoryScopeReservation

                from .lab_scopes import identifier

                reserved = await self.store.repository(
                    LaboratoryScopeReservation,
                    PRINCIPALS[Owner.LABORATORY],
                    execution.command.scope,
                ).get(identifier(execution.command.scope, request.lab_scope))
                if reserved is not None:
                    if reserved.lab_scope != request.lab_scope:
                        raise ContractError(ErrorCode.ACCESS_DENIED)
                    return request.lab_scope
                chosen = await self.lab.scope_factory(label)
                if chosen != request.lab_scope:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                return chosen

            runner = replace(
                self.lab,
                scope_factory=allocated,
                clock=VirtualClock(execution.args.simulation_start),
                lifecycle=lambda lab: self.lab_control.lifecycle(execution, lab),
            )
            lab_report = await runner.run(scenario, seed=request.seed)
            ids.append("labrep-" + lab_report.digest[:40])
        return ids


class Execution:
    def __init__(self, runtime: Runtime, command: ExecutionCommand, lease: Lease) -> None:
        self.runtime, self.command, self.lease = runtime, command, lease
        parsed = runtime.registry.validate(
            command.arguments.schema_id, command.arguments.schema_version, command.arguments.data
        )
        assert isinstance(parsed, JobArguments)
        self.args = parsed
        self.budget: BudgetLease | None = None
        self.cancel = asyncio.Event()
        self.current_attempt: str | None = None
        self.extra_sources: tuple[SourceRef, ...] = ()

    def guard(self) -> None:
        if self.cancel.is_set() or self.runtime.supervisor.clock.utc() >= self.lease.expires_at:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.runtime.supervisor.resolve(
            self.command.cancellation_id, scope=self.command.scope, freshness=self.command.freshness
        )
        if self.budget is not None:
            self.runtime.scheduler.verify(self.budget)

    def allowed(self) -> bool:
        try:
            self.guard()
            return True
        except ContractError:
            return False

    def with_sources(self) -> ExecutionCommand:
        refs = {ref.source_id: ref for ref in (*self.command.sources, *self.extra_sources)}
        return self.command.model_copy(update={"sources": list(refs.values())})

    async def verify(self) -> None:
        await self.runtime.jobs.verify(self.runtime.worker, self.lease)
        from .state import OPERATOR

        for ref in self.extra_sources:
            source = await self.runtime.store.repository(Source, OPERATOR, self.command.scope).get(
                ref.source_id, purpose="api_transfer"
            )
            if source is None or source.version != ref.version:
                raise ContractError(ErrorCode.ACCESS_DENIED)
        self.guard()

    async def renew(self) -> None:
        try:
            renew_at = self.runtime.supervisor.clock.elapsed() + 5
            while True:
                await asyncio.sleep(0.05)
                self.guard()
                if self.runtime.supervisor.clock.elapsed() < renew_at:
                    continue
                renew_at = self.runtime.supervisor.clock.elapsed() + 5
                current = self.lease
                self.lease = await self.runtime.jobs.renew(
                    self.runtime.worker,
                    ServiceLeaseCommand(
                        request_id=uuid4().hex,
                        type="job.renew",
                        scope=current.scope,
                        target=current.operation_id,
                        operation_id=current.operation_id,
                        lease=current,
                    ),
                )
        except ContractError:
            self.cancel.set()

    async def begin(self, report: AttemptReport) -> None:
        self.current_attempt = report.attempt_id
        await self.verify()
        _, _, limits = await self.runtime.policy.permit(self.command.scope, self.args.request.kind)
        ledger = UsageLedger(
            self.runtime.store,
            PRINCIPALS[Owner.LLM],
            self.runtime.registry,
            clock=self.runtime.supervisor.clock.utc,
            reader_access=self.runtime.readers,
        )
        consumed = sum(
            row.operation_id.startswith(self.command.operation_id + ".")
            for row in await ledger.attempts(self.command.scope)
        )
        if consumed >= limits.max_calls:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        await ledger.begin_attempt(
            command(self.command.scope, derived_id("begin", self.command.scope, report.attempt_id)),
            report,
        )
        try:
            await self.verify()
        except BaseException:
            cancelled = replace(
                report,
                status="cancelled",
                failure=Failure.NOT_ADMITTED,
                completed_at=max(self.runtime.supervisor.clock.utc(), report.started_at),
            )
            await ledger.record_attempt(
                command(
                    self.command.scope,
                    derived_id("not-called", self.command.scope, report.attempt_id),
                ),
                cancelled,
            )
            raise

    async def finish(self, report: AttemptReport) -> None:
        # A late technical receipt does not reuse domain admission.
        ledger = UsageLedger(
            self.runtime.store,
            PRINCIPALS[Owner.LLM],
            self.runtime.registry,
            clock=self.runtime.supervisor.clock.utc,
            reader_access=self.runtime.readers,
        )
        await ledger.record_attempt(
            command(
                self.command.scope, derived_id("finish", self.command.scope, report.attempt_id)
            ),
            report,
        )

    async def generate(
        self,
        request: GenerationRequest,
        cancel: asyncio.Event | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Generation:
        await self.verify()
        rows = await self.runtime.state.roles(self.command.scope)
        if request.role_id not in ROLES[self.args.request.kind] or request.role_id not in rows:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        binding = await self.runtime.state.binding(self.command.scope, rows[request.role_id])
        _, _, limits = await self.runtime.policy.permit(self.command.scope, self.args.request.kind)
        if binding.parameters.max_output_tokens > limits.max_output_tokens:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        unique = (
            self.command.operation_id
            + "."
            + hashlib.sha256(
                (self.lease.attempt_id + ":" + request.operation_id).encode()
            ).hexdigest()[:32]
        )
        gateway = LLMGateway(
            self.runtime.providers,
            lambda _: binding,
            journal=self,
            admission=self.allowed,
            clock=self.runtime.supervisor.clock.utc,
            max_attempts=2,
        )
        result = await gateway.generate(
            replace(request, operation_id=unique), self.cancel, on_delta=on_delta
        )
        await self.verify()
        return result


class Classifier:
    def __init__(self, generator: Execution) -> None:
        self.generator = generator

    async def classify(self, text: str, rules: tuple[StoryRule, ...]) -> Classification:
        result = await self.generator.generate(
            GenerationRequest(
                operation_id="story-" + hashlib.sha256(text.encode()).hexdigest()[:40],
                role_id="story-screen",
                messages=(
                    ChatMessage(
                        "system",
                        (
                            "Проверь только данные на сюжетные ограничения. "
                            "Ответ JSON: hard/soft IDs правил, uncertain. Правила: "
                        )
                        + repr(rules),
                    ),
                    ChatMessage("user", text),
                ),
                result_model=StoryVerdict,
            )
        )
        if result.status != "completed" or not isinstance(result.parsed, StoryVerdict):
            return Classification(uncertain=True)
        return Classification(
            tuple(result.parsed.hard), tuple(result.parsed.soft), result.parsed.uncertain
        )
