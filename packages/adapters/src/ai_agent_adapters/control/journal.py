"""Owner-scoped durable run snapshots using the accepted M1-02 transaction port."""

import asyncio
from uuid import uuid4

from ai_agent_application.control.ports import ContextApproval
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import ControlArguments, ControlCommand, Receipt, StopCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import RunSession
from ai_agent_contracts.states import ActivityState, ErrorCode, RunState
from sqlalchemy import select

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key


class SQLiteRunJournal:
    def __init__(self, store: SQLiteStore, principal: Principal, scopes: tuple[Scope, ...]) -> None:
        if (
            principal.role != "core_owner"
            or principal.owner != Owner.SUPERVISOR
            or not scopes
            or len(scopes) > 200
            or len({scope.workspace_id for scope in scopes}) != 1
            or any(scope.agent_id is not None for scope in scopes)
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.store, self.principal = store, principal
        self.scopes = {scope_key(scope): scope for scope in scopes}
        self.workspace_id = scopes[0].workspace_id
        self._lock = asyncio.Lock()

    async def load(self) -> list[RunSession]:
        """Find the single occupied workspace slot, even in another configured persona."""
        await self.store.check_schema()
        async with self.store.connection() as connection:
            active = (
                await connection.execute(
                    select(db.records.c.scope_id, db.records.c.id)
                    .where(
                        db.records.c.kind == "RunSession",
                        db.records.c.active_workspace_id == self.workspace_id,
                    )
                    .limit(2)
                )
            ).all()
        if len(active) > 1:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        result = []
        for key, record_id in active:
            scope = self.scopes.get(str(key))
            if scope is None:
                # Never claim recovery succeeded while an unconfigured profile holds the slot.
                raise ContractError(ErrorCode.ACCESS_DENIED)
            record = await self.store.repository(RunSession, self.principal, scope).get(
                str(record_id)
            )
            if record is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            result.append(record)
        return result

    async def latest(self, scope: Scope) -> RunSession | None:
        """The most recently updated run of this partition, finished or not.

        ``load`` returns only runs that still hold the workspace slot; a finished run is still
        the durable record of who ended it (STOP sets ``ended_at``, a process shutdown does not).
        """
        key = scope_key(scope)
        known = self.scopes.get(key)
        if known is None:
            return None
        await self.store.check_schema()
        async with self.store.connection() as connection:
            row = (
                await connection.execute(
                    select(db.records.c.id)
                    .where(
                        db.records.c.kind == "RunSession",
                        db.records.c.scope_id == key,
                        db.records.c.deleted.is_(False),
                    )
                    .order_by(db.records.c.updated_at.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        return await self.store.repository(RunSession, self.principal, known).get(str(row[0]))

    async def save(self, snapshot: RunSession) -> None:
        key = scope_key(snapshot.scope)
        if key not in self.scopes or snapshot.scope.workspace_id != self.workspace_id:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        async with self._lock:
            previous = await self.store.repository(RunSession, self.principal, snapshot.scope).get(
                snapshot.id
            )
            if previous is not None and previous.scope != snapshot.scope:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            # Durable CAS revision is distinct from the in-memory control projection.
            # Coalescing offline snapshots never skips an immutable DB revision.
            values = snapshot.model_dump()
            values.update(
                version=1 if previous is None else previous.version + 1,
                created_at=snapshot.created_at if previous is None else previous.created_at,
                updated_at=max(
                    self.store.clock(),
                    snapshot.updated_at,
                    previous.updated_at if previous else snapshot.created_at,
                ),
            )
            record = RunSession.model_validate(values)
            request_id = uuid4().hex
            command: StopCommand | ControlCommand
            if snapshot.state in {RunState.STOPPED, RunState.STOPPING, RunState.FAULTED}:
                command = StopCommand(
                    request_id=request_id,
                    scope=snapshot.scope,
                    target=snapshot.agent_id,
                    type="agent.stop",
                )
            else:
                command = ControlCommand(
                    request_id=request_id,
                    scope=snapshot.scope,
                    target=snapshot.agent_id,
                    type=(
                        "agent.pause" if snapshot.activity != ActivityState.OPEN else "agent.start"
                    ),
                    arguments=ControlArguments(autonomy=snapshot.autonomy),
                    expected_version=previous.version if previous else 1,
                )
            async with self.store.transaction(self.principal, snapshot.scope, command) as unit:
                await unit.put(record, expected_version=previous.version if previous else None)
                await unit.complete(
                    Receipt(
                        request_id=request_id,
                        scope=snapshot.scope,
                        status="completed",
                        resource_id=record.id,
                        version=record.version,
                    )
                )


class UnavailableJournal:
    """Default bootstrap cannot activate until explicit storage composition is installed."""

    async def load(self) -> list[RunSession]:
        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)

    async def save(self, snapshot: RunSession) -> None:
        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)


class UnavailableReadiness:
    async def check(self, run: RunSession) -> ContextApproval:
        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)

    async def reconcile(self, previous: RunSession) -> tuple[str, ...]:
        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
