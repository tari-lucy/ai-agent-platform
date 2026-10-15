"""Core-only SQLite adapter. No startup migration, live env, model or network access."""

import builtins
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_agent_application.storage import Grant, Owner, Principal, ReadPurpose, Storage
from ai_agent_contracts.api import CommandBase, PanelCommand, ServiceCommand
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import ExtensionPayload, Record, Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.persona import Opportunity
from ai_agent_contracts.states import ErrorCode
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import URL, and_, delete, event, insert, literal, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from . import schema as db
from .catalogue import SPECS
from .migrations import HEAD, migrate
from .policy import check_principal, scope_key, source_refs, stamp, unexpired, visible

if TYPE_CHECKING:
    from .unit_of_work import SQLiteUnitOfWork


class SQLiteStore(Storage):
    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 5000,
        receipt_retention: timedelta = timedelta(days=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        if not 1 <= busy_timeout_ms <= 60000:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if not timedelta(0) < receipt_retention <= timedelta(days=365):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self._fences: ContextVar[tuple[tuple[Owner | None, Callable[[], None]], ...]] = ContextVar(
            "owner_write_fences", default=()
        )
        self._write_checks: ContextVar[
            tuple[tuple[frozenset[Owner], Callable[[AsyncConnection], Awaitable[None]]], ...]
        ] = ContextVar("transaction_authorization", default=())
        self.receipt_retention = receipt_retention
        self.clock, self.extensions = clock, extensions
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(path.resolve())),
            hide_parameters=True,
            echo=False,
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection: Any, _: Any) -> None:
            connection.isolation_level = None
            cursor = connection.cursor()
            for statement in (
                f"PRAGMA busy_timeout={busy_timeout_ms}",
                "PRAGMA foreign_keys=ON",
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
            ):
                cursor.execute(statement)
            cursor.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def begin(connection: Any) -> None:
            write = connection.get_execution_options().get("write_transaction", False)
            connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")

    @contextmanager
    def fence(self, guard: Callable[[], None], *, owner: Owner | None = None) -> Iterator[None]:
        """Task-local owner write guard; other owners can retain late technical receipts.

        Omitting owner fences every write (trusted administrative checks/tests).
        Nested guards never replace an older guard for the same owner.
        """
        token = self._fences.set((*self._fences.get(), (owner, guard)))
        try:
            yield
        finally:
            self._fences.reset(token)

    @contextmanager
    def authorize_writes(
        self, check: Callable[[AsyncConnection], Awaitable[None]], *, owners: frozenset[Owner]
    ) -> Iterator[None]:
        """Trusted task-local proof, checked inside each existing owner transaction.

        No new rights; read the same SQLite snapshot under its write reservation.
        Technical job/usage receipts can be excluded by the caller.
        """
        token = self._write_checks.set((*self._write_checks.get(), (owners, check)))
        try:
            yield
        finally:
            self._write_checks.reset(token)

    def check_fence(self) -> None:
        for _, guard in self._fences.get():
            guard()

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def connection(self, *, write: bool = False) -> AsyncIterator[AsyncConnection]:
        """Internal adapter hook, not a public handler or worker database API."""
        try:
            async with self.engine.connect() as connection:
                connection = await connection.execution_options(write_transaction=write)
                async with connection.begin():
                    yield connection
        except IntegrityError:
            raise ContractError(ErrorCode.VERSION_CONFLICT) from None
        except OperationalError:
            raise ContractError(ErrorCode.RESOURCE_BUSY) from None

    async def upgrade(self, directory: Path, *, revision: str = "head") -> None:
        """Explicit local administration before starting core; never invoked by reads."""
        async with self.connection(write=True) as connection:
            await connection.run_sync(lambda sync: migrate(sync, directory, revision))

    async def check_schema(self) -> None:
        async with self.connection() as connection:
            try:
                current: list[str] = list(
                    (await connection.execute(text("SELECT version_num FROM alembic_version")))
                    .scalars()
                    .all()
                )
            except OperationalError:
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
            if current != [HEAD]:
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)

    async def empty_test_scope(self, scope: Scope) -> bool:
        """Trusted host metadata check, including hidden/tombstoned rows; never provisions."""
        if scope.memory_mode != "test" or scope.agent_id is not None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        key = scope_key(scope)
        async with self.connection() as connection:
            registered = await connection.scalar(
                select(db.scopes.c.scope_id).where(db.scopes.c.scope_id == key)
            )
            occupied = await connection.scalar(
                select(db.records.c.id).where(db.records.c.scope_id == key).limit(1)
            )
            return registered is not None and occupied is None

    async def provision(self, scope: Scope, grants: tuple[Grant, ...]) -> None:
        """Trusted administrative composition only. Never expose this through client JSON.

        Grant replacement (including an empty tuple) revokes the previous scope grants.
        Owner handlers cannot call this on behalf of a client.
        """
        await self.check_schema()
        for grant in grants:
            check_principal(grant.principal)
            if grant.write and (
                grant.principal.role != "core_owner" or grant.principal.owner != grant.owner
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)
        key = scope_key(scope)
        async with self.connection(write=True) as connection:
            if not await connection.scalar(
                select(db.scopes.c.scope_id).where(db.scopes.c.scope_id == key)
            ):
                await connection.execute(
                    insert(db.scopes).values(
                        scope_id=key,
                        workspace_id=scope.workspace_id,
                        persona_id=scope.persona_id,
                        memory_space_id=scope.memory_space_id,
                        memory_mode=scope.memory_mode,
                    )
                )
            await connection.execute(delete(db.grants).where(db.grants.c.scope_id == key))
            for grant in grants:
                await connection.execute(
                    insert(db.grants).values(
                        scope_id=key,
                        principal_id=grant.principal.id,
                        owner=grant.owner.value,
                        role=grant.principal.role,
                        actor_owner=grant.principal.owner.value if grant.principal.owner else None,
                        read=grant.read,
                        write=grant.write,
                        api_transfer=grant.api_transfer,
                        public_disclosure=grant.public_disclosure,
                        expires_at=stamp(grant.expires_at) if grant.expires_at else None,
                    )
                )

    def repository[T: Record](
        self,
        model: type[T],
        principal: Principal,
        scope: Scope,
    ) -> Repository[T]:
        check_principal(principal)
        spec = SPECS.get(model.__name__)
        if spec is None or spec.model is not model:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return Repository(self, model, principal, scope)

    @asynccontextmanager
    async def transaction(
        self,
        principal: Principal,
        scope: Scope,
        command: CommandBase,
    ) -> AsyncIterator[SQLiteUnitOfWork]:
        from .unit_of_work import SQLiteUnitOfWork

        check_principal(principal)
        if scope != command.scope:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await self.check_schema()
        # Revalidate potentially mutated command lists before fingerprinting.
        wire_type: Any = PanelCommand | ServiceCommand
        adapter: TypeAdapter[CommandBase] = TypeAdapter(wire_type)
        command = from_wire(
            adapter,
            to_wire(command, extensions=self.extensions),
            extensions=self.extensions,
        )
        async with self.connection(write=True) as connection:
            unit = SQLiteUnitOfWork(self, connection, principal, scope, command)
            await unit.begin()
            yield unit
            await unit.finish()


class Repository[T: Record]:
    def __init__(
        self,
        store: SQLiteStore,
        model: type[T],
        principal: Principal,
        scope: Scope,
    ) -> None:
        self.store, self.model, self.principal, self.scope = store, model, principal, scope

    async def _rows(
        self,
        *,
        record_id: str | None = None,
        version: int | None = None,
        purpose: ReadPurpose = "private",
        limit: int = 50,
        after_id: str | None = None,
        lifecycle: bool = False,
        newest: bool = False,
        before: datetime | None = None,
    ) -> list[T]:
        check_principal(self.principal)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if version is not None and (type(version) is not int or version < 1):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if lifecycle and (
            self.model is not Opportunity
            or self.principal.role != "core_owner"
            or self.principal.owner is not Owner.GM
            or purpose != "private"
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await self.store.check_schema()
        now = stamp(self.store.clock())
        revision = db.revisions
        query = (
            select(revision.c.payload)
            .select_from(
                db.records.join(
                    revision,
                    and_(
                        db.records.c.scope_id == revision.c.scope_id,
                        db.records.c.kind == revision.c.kind,
                        db.records.c.id == revision.c.id,
                    ),
                )
            )
            .where(
                db.records.c.scope_id == scope_key(self.scope),
                db.records.c.kind == self.model.__name__,
                revision.c.version == (version if version is not None else db.records.c.version),
                literal(True) if lifecycle else unexpired(revision.c.expires_at, now),
                visible(
                    db.records,
                    revision.c.version,
                    self.principal,
                    purpose,
                    now,
                    lifecycle=lifecycle,
                ),
            )
        )
        if record_id is not None:
            query = query.where(db.records.c.id == record_id)
        if after_id is not None:
            query = query.where(db.records.c.id > after_id)
        if before is not None:
            query = query.where(db.records.c.created_at <= stamp(before))
        ordering = (
            (db.records.c.created_at.desc(), db.records.c.id.desc())
            if newest
            else (db.records.c.id,)
        )
        async with self.store.connection() as connection:
            result = await connection.execute(query.order_by(*ordering).limit(limit))
            decoded = [self._decode(row["payload"]) for row in result.mappings()]
            ref_ids = {
                ref.source_id
                for item in decoded
                for ref in source_refs(item, self.store.extensions)
            }
            expired: set[tuple[str, int]] = set()
            for start in range(0, len(ref_ids), 200):
                batch = sorted(ref_ids)[start : start + 200]
                entries = (
                    await connection.execute(
                        select(
                            db.revisions.c.id,
                            db.revisions.c.version,
                        )
                        .select_from(
                            db.revisions.join(
                                db.records,
                                and_(
                                    db.records.c.scope_id == db.revisions.c.scope_id,
                                    db.records.c.kind == db.revisions.c.kind,
                                    db.records.c.id == db.revisions.c.id,
                                ),
                            )
                        )
                        .where(
                            db.records.c.scope_id == scope_key(self.scope),
                            db.records.c.kind == "Source",
                            db.records.c.id.in_(batch),
                            (db.records.c.expires_at <= now)
                            | (db.revisions.c.expires_at <= now)
                            | (db.records.c.available.is_(False)),
                        )
                    )
                ).all()
                expired.update((str(entry[0]), int(entry[1])) for entry in entries)
            return [self._project_expiry(item, expired) for item in decoded]

    def _decode(self, payload: str) -> T:
        return from_wire(
            TypeAdapter(self.model), payload.encode(), extensions=self.store.extensions
        )

    def _project_expiry(self, model: T, expired: set[tuple[str, int]]) -> T:
        def project(value: Any) -> Any:
            if isinstance(value, SourceRef):
                if (value.source_id, value.version) in expired:
                    return value.model_copy(update={"availability": "expired"})
                return value
            if isinstance(value, ExtensionPayload) and expired:
                registry = self.store.extensions
                if registry is None:
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
                parsed = registry.validate(value.schema_id, value.schema_version, value.data)
                if any(
                    (ref.source_id, ref.version) in expired for ref in source_refs(parsed, registry)
                ):
                    revised = project(parsed)
                    return value.model_copy(update={"data": revised.model_dump(mode="json")})
                return value
            if isinstance(value, BaseModel):
                return value.model_copy(
                    update={
                        name: project(getattr(value, name)) for name in type(value).model_fields
                    }
                )
            if isinstance(value, list):
                return [project(item) for item in value]
            if isinstance(value, dict):
                return {key: project(item) for key, item in value.items()}
            return value

        return TypeAdapter(self.model).validate_python(project(model))

    async def get(
        self,
        record_id: str,
        *,
        purpose: ReadPurpose = "private",
        version: int | None = None,
    ) -> T | None:
        rows = await self._rows(record_id=record_id, version=version, purpose=purpose, limit=1)
        return rows[0] if rows else None

    async def list(
        self,
        *,
        limit: int = 50,
        after_id: str | None = None,
        purpose: ReadPurpose = "private",
    ) -> list[T]:
        return await self._rows(limit=limit, after_id=after_id, purpose=purpose)

    async def recent(
        self,
        *,
        limit: int = 50,
        before: datetime | None = None,
        purpose: ReadPurpose = "private",
    ) -> builtins.list[T]:
        return await self._rows(limit=limit, purpose=purpose, newest=True, before=before)

    async def get_for_lifecycle(self, record_id: str) -> T | None:
        rows = await self._rows(record_id=record_id, lifecycle=True, limit=1)
        return rows[0] if rows else None

    async def list_for_lifecycle(
        self, *, limit: int = 50, after_id: str | None = None
    ) -> builtins.list[T]:
        return await self._rows(limit=limit, after_id=after_id, lifecycle=True)
