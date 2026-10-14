"""Current job projections and bounded TEST observations; JobOwner owns cancellation."""

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.policy import scope_key, stamp, visible
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory.catalogue import catalogue
from ai_agent_application.laboratory.lab import MEMORY, Lab
from ai_agent_application.memory import SourceDraft
from ai_agent_application.memory.service import derived_id
from ai_agent_application.moderation.local import secret_like
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import EditCommand, Receipt
from ai_agent_contracts.codec import ContractError, command_fingerprint, from_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import ExecutionCommand, Job
from ai_agent_contracts.panel import (
    LaboratoryEventArguments,
    LaboratoryRun,
    LaboratoryRuns,
    LaboratoryStopArguments,
)
from ai_agent_contracts.states import ErrorCode, OperationState, Provenance
from pydantic import TypeAdapter
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from .schemas import JobArguments
from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Execution, Runtime


@dataclass
class Live:
    execution: Execution
    lab: Lab
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    count: int = 0
    open: bool = True


class LabControl:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.live: dict[str, Live] = {}
        self.tasks: dict[str, asyncio.Task[Job]] = {}

    async def lifecycle(self, execution: Execution, lab: Lab | None) -> None:
        identifier = execution.command.operation_id
        if lab is not None:
            self.live[identifier] = Live(execution, lab)
        elif (active := self.live.get(identifier)) is not None:
            async with active.lock:
                active.open = False
                self.live.pop(identifier, None)

    async def lifecycle_for_job(self, identifier: str) -> None:
        if (active := self.live.get(identifier)) is not None:
            async with active.lock:
                active.open = False
                self.live.pop(identifier, None)

    async def job(
        self, principal_id: str, scope: Scope, identifier: str
    ) -> tuple[Job, JobArguments]:
        rt = self.runtime
        job = await rt.store.repository(Job, Principal(principal_id, "operator"), scope).get(
            identifier, purpose="api_transfer"
        )
        if job is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        command = await rt.store.repository(
            ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope
        ).get(job.command_id)
        if command is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        args = rt.registry.validate(
            command.arguments.schema_id, command.arguments.schema_version, command.arguments.data
        )
        if (
            not isinstance(args, JobArguments)
            or args.request.kind != "laboratory"
            or args.request.lab_scope is None
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return job, args

    async def read(
        self, principal_id: str, scope: Scope, limit: int, cursor: str | None
    ) -> LaboratoryRuns:
        rt = self.runtime
        rows = await rt.store.repository(Job, Principal(principal_id, "operator"), scope).list(
            limit=limit, after_id=cursor, purpose="api_transfer"
        )
        more = len(rows) == limit
        rows = rows[:limit]
        items = []
        for row in rows:
            if row.capability_id != "m3.laboratory":
                continue
            job, args = await self.job(principal_id, scope, row.operation_id)
            active = self.live.get(job.operation_id)
            ready = bool(
                active
                and active.open
                and active.execution.allowed()
                and job.status == OperationState.RUNNING
            )
            selected = next(
                (s for s in catalogue() if s.scenario_id == args.request.scenario_id), None
            )
            assert args.request.lab_scope is not None
            items.append(
                LaboratoryRun(
                    scope=job.scope,
                    lab_scope=args.request.lab_scope,
                    operation_id=job.operation_id,
                    version=job.version,
                    scenario_id=args.request.scenario_id or "unknown",
                    scenario_version=selected.version if selected else 1,
                    status=job.status,
                    ready=ready,
                    virtual_time=active.lab.clock() if active and ready else None,
                    accepted_events=active.count if active and ready else None,
                )
            )
        return LaboratoryRuns(
            scope=scope,
            items=items,
            limit=limit,
            next_cursor=rows[-1].id if more and rows else None,
            complete=not more,
        )

    async def mutate(self, authority: ControlAuthority, request: EditCommand) -> Receipt:
        rt = self.runtime
        args = rt.registry.validate(
            request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
        )
        is_event = request.type == "laboratory.event" and type(args) is LaboratoryEventArguments
        is_stop = request.type == "laboratory.stop" and type(args) is LaboratoryStopArguments
        if request.target != "laboratory" or not (is_event or is_stop):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        assert isinstance(args, LaboratoryStopArguments)
        job, context = await self.job(authority.principal_id, request.scope, args.operation_id)
        if (
            request.scope != job.scope
            or context.request.lab_scope != args.lab_scope
            or args.lab_scope.partition not in authority.partitions
            or args.lab_scope.partition not in {s.partition for s in rt.scopes}
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

        async def api_guard(connection: AsyncConnection) -> None:
            rt.authorize_configuration(authority, request.scope)
            row = db.records
            allowed = await connection.scalar(
                select(row.c.id).where(
                    row.c.scope_id == scope_key(job.scope),
                    row.c.kind == "Job",
                    row.c.id == job.id,
                    row.c.version == request.expected_version if is_event else true(),
                    visible(
                        row,
                        row.c.version,
                        Principal(authority.principal_id, "operator"),
                        "api_transfer",
                        stamp(rt.store.clock()),
                    ),
                )
            )
            if allowed is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)

        if is_stop:
            try:
                with rt.store.authorize_writes(api_guard, owners=frozenset({Owner.JOBS})):
                    current = await rt.jobs.cancel(authority, request, args.operation_id)
            finally:
                # Only a revoked admission permits interrupting this exact executor.
                task = self.tasks.get(args.operation_id)
                active = self.live.get(args.operation_id)
                if task and active and not active.execution.allowed():
                    task.cancel()
            rt.notify("job.changed", current.scope, current.id, current.version)
            return Receipt(
                request_id=request.request_id,
                scope=request.scope,
                status="completed",
                operation_id=current.operation_id,
                resource_id=current.id,
                version=current.version,
            )
        assert isinstance(args, LaboratoryEventArguments)
        if not args.text.strip() or secret_like(args.text):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        forwarded = request.model_copy(update={"scope": args.lab_scope})
        async with rt.store.connection() as connection:
            previous = (
                (
                    await connection.execute(
                        select(db.operations).where(
                            db.operations.c.scope_id == scope_key(args.lab_scope),
                            db.operations.c.request_id == request.request_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if previous is not None:
            if previous["owner"] != Owner.MEMORY.value or previous["principal_id"] != MEMORY.id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if previous["fingerprint"] != command_fingerprint(forwarded):
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            if previous["expires_at"] <= stamp(rt.store.clock()) or not previous["receipt"]:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            known = from_wire(TypeAdapter(Receipt), previous["receipt"].encode())
            return known.model_copy(
                update={"scope": request.scope, "operation_id": args.operation_id}
            )
        if job.status not in (
            OperationState.QUEUED,
            OperationState.WAITING,
            OperationState.RUNNING,
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        active = self.live.get(args.operation_id)
        if active is None:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        async with active.lock:
            current, _ = await self.job(authority.principal_id, request.scope, args.operation_id)
            if request.expected_version != current.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            if not active.open or current.status != OperationState.RUNNING:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if active.count >= 32:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            await active.execution.verify()
            with ExitStack() as stack:
                stack.enter_context(rt.store.fence(active.execution.guard, owner=Owner.MEMORY))
                stack.enter_context(
                    rt.store.fence(
                        lambda: rt.authorize_configuration(authority, request.scope),
                        owner=Owner.MEMORY,
                    )
                )
                stack.enter_context(
                    rt.store.authorize_writes(
                        lambda connection: rt.policy.transaction_check(
                            active.execution.with_sources(), connection
                        ),
                        owners=frozenset({Owner.MEMORY}),
                    )
                )
                stack.enter_context(
                    rt.store.authorize_writes(api_guard, owners=frozenset({Owner.MEMORY}))
                )
                made = await active.lab.memory.register_source(
                    forwarded,
                    SourceDraft(
                        provenance=Provenance.SIMULATION,
                        source_type="laboratory-operator-event",
                        adapter_id="laboratory",
                        adapter_version="1",
                        occurred_at=active.lab.clock(),
                        content=args.text,
                        read_allowed=True,
                        api_transfer_allowed=False,
                        public_disclosure_allowed=False,
                        author_id=authority.principal_id,
                        external_id=derived_id(
                            "lab-event", args.lab_scope, args.operation_id, request.request_id
                        ),
                    ),
                )
            active.count += 1
            active.lab.note(
                "operator_observation",
                source_id=made.source_id,
                version=made.version,
                sequence=active.count,
            )
            return Receipt(
                request_id=request.request_id,
                scope=request.scope,
                status="completed",
                operation_id=args.operation_id,
                resource_id=made.source_id,
                version=made.version,
            )
