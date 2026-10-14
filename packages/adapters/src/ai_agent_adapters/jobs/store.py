"""Core-only durable jobs; uses the accepted repository/UoW and one SQLite transaction."""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, cast

from ai_agent_application.jobs.owner import TERMINAL
from ai_agent_application.jobs.ports import Attempt, AttemptStatus, DispatchRef, JobPage, Schedule
from ai_agent_application.storage import Owner, Principal, RecordAccess
from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.codec import ContractError, command_fingerprint, from_wire, to_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import (
    ExecutionCommand,
    ExecutionResult,
    Job,
    Lease,
    OperationProjection,
)
from ai_agent_contracts.states import ErrorCode, OperationState
from pydantic import TypeAdapter
from sqlalchemy import and_, func, insert, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import (
    base_visible,
    grant_identity,
    scope_key,
    stamp,
    unexpired,
)
from ai_agent_adapters.storage.unit_of_work import SQLiteUnitOfWork

from . import schema as jobs


class SQLiteJobs:
    def __init__(
        self,
        store: SQLiteStore,
        principal: Principal,
        *,
        reader_access: tuple[RecordAccess, ...] = (),
    ) -> None:
        if principal.role != "core_owner" or principal.owner != Owner.JOBS:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.store, self.principal = store, principal
        self.readers = reader_access

    async def command(self, command_id: str, scope: Scope) -> ExecutionCommand:
        command = await self.store.repository(
            ExecutionCommand,
            self.principal,
            scope,
        ).get(command_id)
        if command is None or command.scope != scope:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return command

    async def result(self, result_id: str, scope: Scope) -> ExecutionResult | None:
        return await self.store.repository(ExecutionResult, self.principal, scope).get(result_id)

    async def load(self, operation_id: str, scope: Scope) -> tuple[Job, Schedule] | None:
        async with self.store.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(jobs.schedules).where(
                            jobs.schedules.c.scope_id == scope_key(scope),
                            jobs.schedules.c.operation_id == operation_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        job = await self.store.repository(Job, self.principal, scope).get(row["job_id"])
        if job is None or job.scope != scope:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return job, Schedule(
            job.id,
            operation_id,
            scope,
            row["fingerprint"],
            row["attempts"],
            datetime.fromisoformat(row["ready_at"]),
            row["retry_safe"],
        )

    def _current(self) -> Any:
        return db.records.join(
            db.revisions,
            and_(
                db.records.c.scope_id == db.revisions.c.scope_id,
                db.records.c.kind == db.revisions.c.kind,
                db.records.c.id == db.revisions.c.id,
                db.records.c.version == db.revisions.c.version,
            ),
        ).join(
            jobs.schedules,
            and_(
                db.records.c.scope_id == jobs.schedules.c.scope_id,
                db.records.c.id == jobs.schedules.c.job_id,
            ),
        )

    async def active(self, *, limit: int, cursor: str | None = None) -> JobPage:
        if not 1 <= limit <= 200:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        page_filter: Any = jobs.schedules.c.operation_id.is_not(None)
        if cursor is not None:
            parts = cursor.split("|", 1)
            if len(parts) != 2 or len(parts[0]) != 64 or len(parts[1]) > 128:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            page_filter = tuple_(jobs.schedules.c.scope_id, jobs.schedules.c.operation_id) > tuple(
                parts
            )
        async with self.store.connection() as connection:
            rows = (
                (
                    await connection.execute(
                        select(
                            db.revisions.c.payload,
                            jobs.schedules,
                        )
                        .select_from(self._current())
                        .where(
                            page_filter,
                            db.records.c.kind == "Job",
                            base_visible(
                                db.records, self.principal, "private", stamp(self.store.clock())
                            ),
                            func.json_extract(db.revisions.c.payload, "$.status").not_in(
                                [item.value for item in TERMINAL],
                            ),
                        )
                        .order_by(jobs.schedules.c.scope_id, jobs.schedules.c.operation_id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            job = from_wire(
                TypeAdapter(Job),
                row["payload"].encode(),
                extensions=self.store.extensions,
            )
            # Repository rechecks scope grant/source visibility; metadata doesn't grant access.
            stored = await self.load(job.operation_id, job.scope)
            if stored is not None:
                items.append(stored)
        next_cursor = (
            rows[-1]["scope_id"] + "|" + rows[-1]["operation_id"] if len(rows) == limit else None
        )
        return JobPage(items, next_cursor)

    async def receipt(self, command: CommandBase) -> Receipt | None:
        to_wire(command, extensions=self.store.extensions)
        fingerprint = command_fingerprint(command)
        async with self.store.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(db.operations).where(
                            db.operations.c.scope_id == scope_key(command.scope),
                            db.operations.c.request_id == command.request_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        if row["owner"] != "jobs" or row["principal_id"] != self.principal.id:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if row["fingerprint"] != fingerprint:
            raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
        if row["expires_at"] <= stamp(self.store.clock()) or row["receipt"] is None:
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        return from_wire(TypeAdapter(Receipt), row["receipt"].encode())

    async def attempt(self, operation_id: str, attempt_id: str, scope: Scope) -> Attempt | None:
        stored = await self.load(operation_id, scope)
        if stored is None:
            return None
        job, _ = stored
        async with self.store.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(jobs.attempts).where(
                            jobs.attempts.c.scope_id == scope_key(scope),
                            jobs.attempts.c.operation_id == operation_id,
                            jobs.attempts.c.attempt_id == attempt_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        lease = Lease(
            operation_id=operation_id,
            command_id=job.command_id,
            attempt_id=attempt_id,
            lease_version=row["lease_version"],
            worker_id=row["worker_id"],
            scope=scope,
            freshness=job.freshness,
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )
        return Attempt(
            lease,
            cast(AttemptStatus, row["status"]),
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            ErrorCode(row["error_code"]) if row["error_code"] else None,
        )

    async def reconciliation(self, receipt_id: str, scope: Scope) -> str | None:
        # Recheck owner grant through the authorized job repository before metadata.
        async with self.store.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(jobs.reconciliations).where(
                            jobs.reconciliations.c.scope_id == scope_key(scope),
                            jobs.reconciliations.c.receipt_id == receipt_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        await self.load(row["operation_id"], scope)
        return str(row["fingerprint"])

    @asynccontextmanager
    async def transaction(
        self,
        command: CommandBase,
        *,
        pending_limit: int,
    ) -> AsyncIterator[SQLiteJobTransaction]:
        async with self.store.transaction(self.principal, command.scope, command) as unit:
            yield SQLiteJobTransaction(self, unit, pending_limit)

    async def pending_events(self, *, limit: int) -> list[DispatchRef]:
        if not 1 <= limit <= 200:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        now = stamp(self.store.clock())
        async with self.store.connection() as connection:
            rows = (
                (
                    await connection.execute(
                        select(db.outbox)
                        .select_from(
                            db.outbox.join(
                                db.records,
                                and_(
                                    db.outbox.c.scope_id == db.records.c.scope_id,
                                    db.outbox.c.kind == db.records.c.kind,
                                    db.outbox.c.resource_id == db.records.c.id,
                                    db.outbox.c.resource_version == db.records.c.version,
                                ),
                            )
                            .join(
                                db.revisions,
                                and_(
                                    db.records.c.scope_id == db.revisions.c.scope_id,
                                    db.records.c.kind == db.revisions.c.kind,
                                    db.records.c.id == db.revisions.c.id,
                                    db.records.c.version == db.revisions.c.version,
                                ),
                            )
                            .join(
                                jobs.schedules,
                                and_(
                                    db.records.c.scope_id == jobs.schedules.c.scope_id,
                                    db.records.c.id == jobs.schedules.c.job_id,
                                ),
                            )
                        )
                        .where(
                            db.outbox.c.owner == "jobs",
                            db.outbox.c.kind == "Job",
                            db.outbox.c.status.in_(["pending", "delivered"]),
                            unexpired_due(db.outbox.c.next_attempt_at, now),
                            jobs.schedules.c.ready_at <= now,
                            func.json_extract(db.revisions.c.payload, "$.deadline") > now,
                            func.json_extract(db.revisions.c.payload, "$.status").in_(
                                ["QUEUED", "WAITING"]
                            ),
                            base_visible(db.records, self.principal, "private", now),
                        )
                        .order_by(db.outbox.c.created_at, db.outbox.c.event_id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [
            DispatchRef(
                row["event_id"],
                row["operation_id"],
                row["resource_id"],
                row["resource_version"],
                from_wire(TypeAdapter(Scope), row["scope"].encode()),
            )
            for row in rows
        ]

    async def _delivery(
        self,
        reference: DispatchRef,
        status: str,
        retry_at: datetime,
    ) -> None:
        async with self.store.connection(write=True) as connection:
            allowed = await connection.scalar(
                select(db.grants.c.owner).where(
                    db.grants.c.scope_id == scope_key(reference.scope),
                    db.grants.c.owner == "jobs",
                    grant_identity(db.grants, self.principal),
                    db.grants.c.write.is_(True),
                    unexpired(db.grants.c.expires_at, stamp(self.store.clock())),
                )
            )
            if allowed is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            await connection.execute(
                update(db.outbox)
                .where(
                    db.outbox.c.event_id == reference.event_id,
                    db.outbox.c.scope_id == scope_key(reference.scope),
                    db.outbox.c.owner == "jobs",
                )
                .values(
                    status=status,
                    delivery_count=db.outbox.c.delivery_count + 1,
                    next_attempt_at=stamp(retry_at),
                )
            )

    async def dispatched(self, reference: DispatchRef, *, retry_at: datetime) -> None:
        await self._delivery(reference, "delivered", retry_at)

    async def delivery_failed(self, reference: DispatchRef, *, retry_at: datetime) -> None:
        # Finite per-job deadline, backoff, bounded pass. Never unbounded in-memory retries.
        await self._delivery(reference, "pending", retry_at)


def unexpired_due(column: Any, now: str) -> Any:
    return (column.is_(None)) | (column <= now)


class SQLiteJobTransaction:
    def __init__(self, owner: SQLiteJobs, unit: SQLiteUnitOfWork, pending_limit: int) -> None:
        self.owner, self.unit, self.pending_limit = owner, unit, pending_limit

    async def save(
        self,
        job: Job,
        schedule: Schedule,
        *,
        expected_version: int | None,
        attempt: Attempt | None = None,
        result: ExecutionResult | None = None,
        reconciliation: ExecutionResult | None = None,
    ) -> None:
        if self.unit.replay is not None:
            raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
        connection = self.unit.connection
        if expected_version is None:
            count = await connection.scalar(
                select(func.count())
                .select_from(
                    self.owner._current().join(
                        db.scopes, db.records.c.scope_id == db.scopes.c.scope_id
                    )
                )
                .where(
                    db.scopes.c.workspace_id == job.scope.workspace_id,
                    db.records.c.kind == "Job",
                    func.json_extract(db.revisions.c.payload, "$.status").not_in(
                        [item.value for item in TERMINAL if item.value != "OUTCOME_UNKNOWN"],
                    ),
                )
            )
            if (count or 0) >= self.pending_limit:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
        await self.unit.put(
            job, expected_version=expected_version, access=self.owner.readers or None
        )
        # Same owner transaction: a panel status never lags its durable job.
        old_projection = await connection.scalar(
            select(db.records.c.version).where(
                db.records.c.scope_id == scope_key(job.scope),
                db.records.c.kind == "OperationProjection",
                db.records.c.id == job.operation_id,
            )
        )
        observed = result or reconciliation
        result_id = observed.id if observed else None
        if (
            result_id is None
            and job.status == OperationState.SUCCEEDED
            and old_projection is not None
        ):
            # A factual late ACK must preserve the already accepted result.
            previous = await connection.scalar(
                select(db.revisions.c.payload).where(
                    db.revisions.c.scope_id == scope_key(job.scope),
                    db.revisions.c.kind == "OperationProjection",
                    db.revisions.c.id == job.operation_id,
                    db.revisions.c.version == old_projection,
                )
            )
            result_id = json.loads(previous)["result_id"] if previous else None
        projection = OperationProjection(
            id=job.operation_id,
            scope=job.scope,
            created_at=job.created_at,
            updated_at=job.updated_at,
            version=(old_projection or 0) + 1,
            operation_id=job.operation_id,
            status=job.status,
            deadline=job.deadline,
            waiting_reason=job.waiting_reason,
            wake_event=job.wake_event,
            result_id=result_id,
        )
        await self.unit.put(
            projection, expected_version=old_projection, access=self.owner.readers or None
        )
        values = dict(
            scope_id=scope_key(job.scope),
            operation_id=job.operation_id,
            job_id=job.id,
            fingerprint=schedule.fingerprint,
            attempts=schedule.attempts,
            ready_at=stamp(schedule.ready_at),
            retry_safe=schedule.retry_safe,
        )
        if expected_version is None:
            await connection.execute(insert(jobs.schedules).values(**values))
        else:
            await connection.execute(
                update(jobs.schedules)
                .where(
                    jobs.schedules.c.scope_id == scope_key(job.scope),
                    jobs.schedules.c.operation_id == job.operation_id,
                )
                .values(**values)
            )
        if attempt is not None:
            lease = attempt.lease
            data = dict(
                scope_id=scope_key(job.scope),
                operation_id=job.operation_id,
                attempt_id=lease.attempt_id,
                lease_version=lease.lease_version,
                worker_id=lease.worker_id,
                expires_at=stamp(lease.expires_at),
                status=attempt.status,
                finished_at=stamp(attempt.finished_at) if attempt.finished_at else None,
                error_code=attempt.error.value if attempt.error else None,
            )
            await connection.execute(
                sqlite_insert(jobs.attempts)
                .values(**data)
                .on_conflict_do_update(
                    index_elements=["scope_id", "operation_id", "attempt_id"],
                    set_=data,
                )
            )
        if result is not None:
            await self.unit.put(result)
        if reconciliation is not None:
            if reconciliation.result is not None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            for ref in reconciliation.evidence:
                await self.unit.validate_reference(ref)
                self.unit.references.append(ref)
            wire = to_wire(reconciliation, extensions=self.owner.store.extensions)
            await connection.execute(
                insert(jobs.reconciliations).values(
                    scope_id=scope_key(job.scope),
                    receipt_id=reconciliation.id,
                    operation_id=job.operation_id,
                    attempt_id=reconciliation.attempt_id,
                    fingerprint=hashlib.sha256(wire).hexdigest(),
                    metadata_json=json.dumps(
                        reconciliation.model_dump(mode="json", exclude={"result"}),
                        separators=(",", ":"),
                    ),
                )
            )
        # Old versions aren't new deliveries. Result events belong to downstream owners,
        # so only obsolete Job references are settled here, never another owner's outbox.
        await connection.execute(
            update(db.outbox)
            .where(
                db.outbox.c.owner == "jobs",
                db.outbox.c.kind == "Job",
                db.outbox.c.scope_id == scope_key(job.scope),
                db.outbox.c.resource_id == job.id,
                db.outbox.c.resource_version < job.version,
            )
            .values(status="delivered", next_attempt_at=None)
        )
        await self.unit.complete(
            Receipt(
                request_id=self.unit.command.request_id,
                scope=job.scope,
                status="completed",
                operation_id=job.operation_id,
                resource_id=job.id,
                version=job.version,
            )
        )
