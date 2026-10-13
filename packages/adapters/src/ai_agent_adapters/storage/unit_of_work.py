"""One owner transaction: versions, exact uniqueness, receipt and outbox commit together."""

from datetime import datetime
from typing import TYPE_CHECKING, NoReturn
from uuid import uuid4

from ai_agent_application.storage import Principal, RecordAccess
from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.codec import ContractError, command_fingerprint, from_wire, to_wire
from ai_agent_contracts.common import Record, Scope, SourceRef, VersionedRecord
from ai_agent_contracts.control import RunSession
from ai_agent_contracts.memory import Source
from ai_agent_contracts.persona import StateChange
from ai_agent_contracts.states import ErrorCode, RunState
from pydantic import TypeAdapter
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from . import schema as db
from .catalogue import SPECS, semantic_keys
from .policy import (
    base_visible,
    grant_identity,
    scope_key,
    source_refs,
    stamp,
    unexpired,
)

if TYPE_CHECKING:
    from .database import SQLiteStore


class SQLiteUnitOfWork:
    def __init__(
        self,
        store: SQLiteStore,
        connection: AsyncConnection,
        principal: Principal,
        scope: Scope,
        command: CommandBase,
    ) -> None:
        self.store, self.connection, self.principal = store, connection, principal
        self.scope, self.command = scope, command
        self.fingerprint = command_fingerprint(command)
        self.key = scope_key(scope)
        self.owner = principal.owner
        self.replay: Receipt | None = None
        self.completed = False
        self.failed = False
        self.mutation_count = 0
        self.written: dict[str, int] = {}
        self.references: list[SourceRef] = []

    def fail(self, code: ErrorCode, current: int | None = None) -> NoReturn:
        self.failed = True
        raise ContractError(code, self.command.request_id, current)

    async def authorize(self) -> None:
        if self.principal.role != "core_owner" or self.owner is None:
            self.fail(ErrorCode.ACCESS_DENIED)
        allowed = await self.connection.scalar(
            select(db.grants.c.owner).where(
                db.grants.c.scope_id == self.key,
                db.grants.c.owner == self.owner.value,
                grant_identity(db.grants, self.principal),
                db.grants.c.write.is_(True),
                unexpired(db.grants.c.expires_at, stamp(self.store.clock())),
            )
        )
        if allowed is None:
            self.fail(ErrorCode.ACCESS_DENIED)

    async def begin(self) -> None:
        await self.authorize()
        previous = (
            (
                await self.connection.execute(
                    select(db.operations).where(
                        db.operations.c.scope_id == self.key,
                        db.operations.c.request_id == self.command.request_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if previous:
            if previous["owner"] != self.owner or previous["principal_id"] != self.principal.id:
                self.fail(ErrorCode.ACCESS_DENIED)
            if previous["fingerprint"] != self.fingerprint:
                self.fail(ErrorCode.IDEMPOTENCY_CONFLICT)
            if previous["expires_at"] <= stamp(self.store.clock()) or previous["receipt"] is None:
                self.fail(ErrorCode.OUTCOME_UNKNOWN)
            self.replay = from_wire(TypeAdapter(Receipt), previous["receipt"].encode())

    async def validate_reference(self, ref: SourceRef) -> None:
        if ref.scope.partition != self.scope.partition:
            self.fail(ErrorCode.ACCESS_DENIED)
        if ref.availability != "available":
            return  # Retain only provenance metadata; reader suppresses the derived record.
        now = stamp(self.store.clock())
        revision = db.revisions
        valid = await self.connection.scalar(
            select(db.records.c.id)
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
                db.records.c.scope_id == self.key,
                db.records.c.kind == "Source",
                db.records.c.id == ref.source_id,
                revision.c.version == ref.version,
                unexpired(revision.c.expires_at, now),
                func.json_extract(revision.c.payload, "$.availability") == "available",
                base_visible(db.records, self.principal, "private", now),
            )
        )
        if valid is None:
            self.fail(ErrorCode.ACCESS_DENIED)

    def identity(self, kind: str, record_id: str) -> tuple[ColumnElement[bool], ...]:
        return (
            db.records.c.scope_id == self.key,
            db.records.c.kind == kind,
            db.records.c.id == record_id,
        )

    async def emit(self, kind: str, record_id: str, version: int, operation_id: str) -> None:
        await self.connection.execute(
            insert(db.outbox).values(
                event_id=str(uuid4()),
                scope=to_wire(self.scope).decode(),
                operation_id=operation_id,
                causal_id=self.command.request_id,
                scope_id=self.key,
                owner=self.owner.value if self.owner else "",
                kind=kind,
                resource_id=record_id,
                resource_version=version,
                request_id=self.command.request_id,
                created_at=stamp(self.store.clock()),
                status="pending",
                delivery_count=0,
                next_attempt_at=None,
            )
        )

    async def put(
        self,
        record: Record,
        *,
        expected_version: int | None = None,
        access: tuple[RecordAccess, ...] | None = None,
    ) -> None:
        try:
            await self._put(record, expected_version=expected_version, access=access)
        except IntegrityError:
            self.failed = True
            raise ContractError(ErrorCode.VERSION_CONFLICT, self.command.request_id) from None
        except BaseException:
            self.failed = True
            raise

    async def _put(
        self,
        record: Record,
        *,
        expected_version: int | None = None,
        access: tuple[RecordAccess, ...] | None = None,
    ) -> None:
        if self.failed or self.replay is not None or self.completed:
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        if self.mutation_count >= 200:
            self.fail(ErrorCode.RESOURCE_BUSY)
        await self.authorize()
        kind = type(record).__name__
        spec = SPECS.get(kind)
        if spec is None or spec.model is not type(record):
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        if record.scope != self.scope:
            self.fail(ErrorCode.ACCESS_DENIED)
        if spec.owner is not None and spec.owner != self.owner:
            self.fail(ErrorCode.ACCESS_DENIED)
        if isinstance(record, StateChange) and record.owner != self.owner:
            self.fail(ErrorCode.ACCESS_DENIED)
        payload = to_wire(record, extensions=self.store.extensions).decode()
        # Own a validated snapshot; caller mutation during an awaited write cannot alter keys/ACL.
        record = from_wire(
            TypeAdapter(type(record)), payload.encode(), extensions=self.store.extensions
        )
        refs = source_refs(record, self.store.extensions)
        for ref in refs:
            await self.validate_reference(ref)
        self.references.extend(refs)
        current = (
            (
                await self.connection.execute(
                    select(db.records).where(*self.identity(kind, record.id))
                )
            )
            .mappings()
            .first()
        )
        version = record.version if isinstance(record, VersionedRecord) else 1
        if current is None:
            if expected_version is not None or version != 1:
                self.fail(ErrorCode.VERSION_CONFLICT)
        else:
            if current["deleted"] or current["owner"] != self.owner:
                self.fail(ErrorCode.ACCESS_DENIED)
            if (
                not isinstance(record, VersionedRecord)
                or type(expected_version) is not int
                or expected_version != current["version"]
                or version != expected_version + 1
            ):
                self.fail(ErrorCode.VERSION_CONFLICT, current["version"])
            if (
                stamp(record.created_at) != current["created_at"]
                or stamp(record.updated_at) < current["updated_at"]
            ):
                self.fail(ErrorCode.INVALID_ARGUMENTS)
        expiry = getattr(record, "expires_at", None)
        if isinstance(record, Source):
            expiry = record.retention.expires_at
        removed = isinstance(record, Source) and record.availability == "deleted"
        values = dict(
            active_workspace_id=self.scope.workspace_id
            if isinstance(record, RunSession) and record.state != RunState.STOPPED
            else None,
            owner=self.owner.value if self.owner else "",
            version=version,
            created_at=stamp(record.created_at),
            updated_at=stamp(
                record.updated_at if isinstance(record, VersionedRecord) else record.created_at
            ),
            expires_at=stamp(expiry) if isinstance(expiry, datetime) else None,
            deleted=removed,
            available=not isinstance(record, Source) or record.availability == "available",
            read_allowed=record.read_allowed if isinstance(record, Source) else True,
            api_transfer_allowed=record.api_transfer_allowed
            if isinstance(record, Source)
            else True,
            public_disclosure_allowed=record.public_disclosure_allowed
            if isinstance(record, Source)
            else True,
        )
        if current is None:
            await self.connection.execute(
                insert(db.records).values(scope_id=self.key, kind=kind, id=record.id, **values)
            )
        else:
            await self.connection.execute(
                update(db.records)
                .where(*self.identity(kind, record.id), db.records.c.version == expected_version)
                .values(**values)
            )
        if removed:
            await self.connection.execute(
                delete(db.links).where(
                    db.links.c.scope_id == self.key,
                    db.links.c.kind == kind,
                    db.links.c.id == record.id,
                )
            )
            await self.connection.execute(
                delete(db.revisions).where(
                    db.revisions.c.scope_id == self.key,
                    db.revisions.c.kind == kind,
                    db.revisions.c.id == record.id,
                )
            )
        await self.connection.execute(
            insert(db.revisions).values(
                scope_id=self.key,
                kind=kind,
                id=record.id,
                version=version,
                schema_version=record.schema_version,
                payload=payload,
                expires_at=values["expires_at"],
            )
        )
        # Removed identities retain exact keys; re-import cannot resurrect the same identity.
        if not removed:
            await self.connection.execute(
                delete(db.keys).where(
                    db.keys.c.scope_id == self.key,
                    db.keys.c.kind == kind,
                    db.keys.c.id == record.id,
                )
            )
            for namespace, key in semantic_keys(record):
                await self.connection.execute(
                    insert(db.keys).values(
                        scope_id=self.key, namespace=namespace, key=key, kind=kind, id=record.id
                    )
                )
        for ref in {(r.source_id, r.version): r for r in refs}.values():
            await self.connection.execute(
                insert(db.links).values(
                    scope_id=self.key,
                    kind=kind,
                    id=record.id,
                    version=version,
                    source_id=ref.source_id,
                    source_version=ref.version,
                    availability=ref.availability,
                )
            )
        if access is not None:
            if len(access) > 200:
                self.fail(ErrorCode.INVALID_ARGUMENTS)
            await self.connection.execute(
                delete(db.access).where(
                    db.access.c.scope_id == self.key,
                    db.access.c.kind == kind,
                    db.access.c.id == record.id,
                )
            )
            for permission in access:
                await self.connection.execute(
                    insert(db.access).values(
                        scope_id=self.key,
                        kind=kind,
                        id=record.id,
                        principal_id=permission.principal_id,
                        read=permission.read,
                        api_transfer=permission.api_transfer,
                        public_disclosure=permission.public_disclosure,
                        expires_at=stamp(permission.expires_at) if permission.expires_at else None,
                    )
                )
        logical_id = str(
            getattr(
                record,
                "operation_id",
                getattr(self.command, "operation_id", self.command.request_id),
            )
        )
        await self.emit(kind, record.id, version, logical_id)
        self.written[record.id] = version
        self.mutation_count += 1

    async def complete(self, receipt: Receipt) -> None:
        if self.failed or self.replay is not None or self.completed:
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        if type(receipt) is not Receipt:
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        if receipt.request_id != self.command.request_id or receipt.scope != self.scope:
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        if receipt.resource_id is not None and (
            receipt.resource_id not in self.written
            or receipt.version != self.written[receipt.resource_id]
        ):
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        await self.authorize()
        payload = to_wire(receipt).decode()
        await self.connection.execute(
            insert(db.operations).values(
                scope_id=self.key,
                request_id=self.command.request_id,
                owner=self.owner.value if self.owner else "",
                principal_id=self.principal.id,
                command_type=str(getattr(self.command, "type", "")),
                fingerprint=self.fingerprint,
                receipt=payload,
                receipt_id=str(uuid4()),
                expires_at=stamp(self.store.clock() + self.store.receipt_retention),
                created_at=stamp(self.store.clock()),
            )
        )
        self.completed = True

    async def finish(self) -> None:
        if self.failed:
            self.fail(ErrorCode.INVALID_ARGUMENTS)
        await self.authorize()
        for ref in self.references:
            await self.validate_reference(ref)
        if self.replay is None and not self.completed:
            self.fail(ErrorCode.INVALID_ARGUMENTS)
