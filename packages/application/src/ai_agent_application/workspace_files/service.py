"""ManagedFiles implements the accepted AW-00 WorkspaceFiles port over existing UoW."""

import asyncio
import builtins
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from ai_agent_contracts.agent_workplace import (
    WRITABLE_AREAS,
    FileAccess,
    FileWrite,
    ManagedFile,
    logical_path,
)
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError, to_wire
from ai_agent_contracts.common import ArtifactRef, Scope
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.agent_workplace.ports import AttemptContext, WorkAdmission
from ai_agent_application.storage import Owner, Principal, ReadPurpose, RecordAccess, Storage

from .guard import FileReleaseGuard, current, stable_scope

# Areas the operator may upload into through the trusted path: materials for the person.
# exports is the agent-to-operator direction, scratch is the person's own TTL experiments.
OPERATOR_AREAS = frozenset({"inbox", "knowledge", "projects"})


class BlobStore(Protocol):
    async def put(self, scope: Scope, digest: str, data: bytes, max_scope_bytes: int) -> None: ...
    async def read(self, scope: Scope, digest: str, max_bytes: int) -> bytes: ...
    async def used_bytes(self, scope: Scope) -> int: ...


type FileAuthorizer = Callable[[WorkAdmission, Literal["read", "write"], str], Awaitable[None]]


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes: int
    max_scope_bytes: int
    max_files: int
    max_versions: int
    max_scan: int = 2000

    def __post_init__(self) -> None:
        values = (
            self.max_file_bytes,
            self.max_scope_bytes,
            self.max_files,
            self.max_versions,
            self.max_scan,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("FILE_LIMIT_INVALID")
        if self.max_file_bytes > self.max_scope_bytes:
            raise ValueError("FILE_LIMIT_INVALID")


class ManagedFiles:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        blobs: BlobStore,
        guard: FileReleaseGuard,
        attempts: AttemptContext,
        usage: Callable[[Scope], Awaitable[tuple[int, int]]],
        limits: FileLimits,
        *,
        authorize: FileAuthorizer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reader_access: tuple[RecordAccess, ...] = (),
        default_access: FileAccess | None = None,
    ) -> None:
        if principal.role != "core_owner" or principal.owner is not Owner.CATALOGUE:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.storage, self.principal, self.blobs = storage, principal, blobs
        self.guard, self.attempts, self.usage, self.limits = guard, attempts, usage, limits
        self.clock, self.reader_access = clock, reader_access
        self.default_access = default_access or FileAccess(
            api_transfer=False, public_disclosure=False
        )
        self.authorize = authorize
        self._writes = asyncio.Lock()

    async def _file(self, scope: Scope, file_id: str, version: int | None = None) -> ManagedFile:
        entry = await self.storage.repository(ManagedFile, self.principal, stable_scope(scope)).get(
            file_id, version=version
        )
        if entry is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await self.guard.check(scope, (), (entry.reference,))
        return entry

    async def _content(self, entry: ManagedFile) -> bytes:
        try:
            data = await self.blobs.read(
                entry.scope, entry.reference.checksum, self.limits.max_file_bytes
            )
        except OSError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
        if len(data) != entry.reference.size_bytes:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        await self.guard.check(entry.scope, (), (entry.reference,))
        return data

    async def read(
        self, admission: WorkAdmission, file_id: str, version: int | None = None
    ) -> tuple[ManagedFile, bytes]:
        current(admission)
        entry = await self._file(admission.scope, file_id, version)
        await self.authorize(admission, "read", entry.area)
        await self.guard.verify(admission, (), (entry.reference,), "private")
        data = await self._content(entry)
        await self.guard.verify(admission, (), (entry.reference,), "private")
        await self.authorize(admission, "read", entry.area)
        return entry, data

    async def released_content(
        self, scope: Scope, file_id: str, purpose: ReadPurpose
    ) -> tuple[ManagedFile, bytes]:
        """Current file content for a caller authenticated by the composition boundary."""
        entry = await self._file(scope, file_id)
        await self.guard.check(scope, (), (entry.reference,), purpose)
        data = await self._content(entry)
        await self.guard.check(scope, (), (entry.reference,), purpose)
        latest = await self._file(scope, file_id)
        if latest.reference != entry.reference:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return entry, data

    async def _entries(self, scope: Scope) -> list[ManagedFile]:
        found: list[ManagedFile] = []
        cursor = None
        repo = self.storage.repository(ManagedFile, self.principal, stable_scope(scope))
        while True:
            page = await repo.list(limit=200, after_id=cursor)
            found.extend(page)
            if len(found) > self.limits.max_scan:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            if len(page) < 200:
                return found
            cursor = page[-1].id

    async def list(
        self, scope: Scope, area: str, *, prefix: str | None = None, limit: int = 50
    ) -> list[ManagedFile]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 200
            or area not in WRITABLE_AREAS | {"reference", "guide"}
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if prefix is not None:
            try:
                logical_path(prefix.rstrip("/"))
            except ValueError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        results = []
        for entry in await self._entries(scope):
            if entry.area != area or prefix is not None and not entry.path.startswith(prefix):
                continue
            try:
                await self.guard.check(scope, (), (entry.reference,))
            except ContractError as error:
                if error.error.code in {
                    ErrorCode.ACCESS_DENIED,
                    ErrorCode.EXPIRED,
                    ErrorCode.STALE_CONTEXT,
                }:
                    continue
                raise
            results.append(entry)
            if len(results) == limit:
                break
        return results

    async def search(
        self, scope: Scope, query: str, *, limit: int = 20
    ) -> builtins.list[ManagedFile]:
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 512
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        needle = query.strip().casefold()
        results = []
        for entry in await self._entries(scope):
            try:
                await self.guard.check(scope, (), (entry.reference,))
                matches = needle in (entry.title + " " + entry.path).casefold()
                if not matches and (
                    entry.reference.media_type.startswith("text/")
                    or entry.reference.media_type == "application/json"
                ):
                    matches = (
                        needle
                        in (await self._content(entry)).decode("utf-8", errors="replace").casefold()
                    )
                if matches:
                    results.append(entry)
            except ContractError as error:
                if error.error.code not in {
                    ErrorCode.ACCESS_DENIED,
                    ErrorCode.EXPIRED,
                    ErrorCode.STALE_CONTEXT,
                }:
                    raise
            if len(results) == limit:
                break
        return results

    async def write(
        self, admission: WorkAdmission, change: FileWrite, content: bytes
    ) -> ManagedFile:
        current(admission)
        change = FileWrite.model_validate_json(change.model_dump_json())
        await self.authorize(admission, "write", change.area)
        if (
            not isinstance(content, bytes)
            or len(content) != change.size_bytes
            or hashlib.sha256(content).hexdigest() != change.checksum
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if len(content) > self.limits.max_file_bytes:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if change.attempt_id not in (None, admission.attempt_id):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        area = stable_scope(admission.scope)
        fingerprint = hashlib.sha256(to_wire(change)).hexdigest()
        command = MessageCommand(
            request_id=change.request_id,
            scope=area,
            target=change.file_id,
            type="dialogue.message",
            arguments=MessageArguments(text="workspace-file:" + fingerprint),
        )
        async with self._writes:
            with self.storage.fence(lambda: current(admission), owner=Owner.CATALOGUE):
                async with self.storage.transaction(self.principal, area, command) as unit:
                    if unit.replay is not None:
                        if unit.replay.resource_id != change.file_id or unit.replay.version is None:
                            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
                        return await self._file(area, change.file_id, unit.replay.version)
                    previous = None
                    if change.expected_version is not None:
                        previous = await self._file(area, change.file_id)
                        if previous.version != change.expected_version:
                            raise ContractError(ErrorCode.VERSION_CONFLICT)
                        if previous.area not in WRITABLE_AREAS:
                            raise ContractError(ErrorCode.ACCESS_DENIED)
                        if previous.area != change.area:
                            await self.authorize(admission, "write", previous.area)
                    attempt = await self.attempts.attempt(admission.scope, admission.attempt_id)
                    if (
                        attempt is None
                        or attempt.scope != admission.scope
                        or attempt.freshness != admission.freshness
                        or attempt.operation_id != admission.operation_id
                        or attempt.state not in {"prepared", "submitted"}
                    ):
                        raise ContractError(ErrorCode.STALE_CONTEXT)
                    refs = [*attempt.inputs, *change.declared_sources]
                    parents = list(attempt.input_files)
                    if previous is not None:
                        refs.extend(previous.dependencies)
                        parents.extend(previous.derived_from)
                    for parent in parents:
                        source_file = await self._file(area, parent.artifact_id, parent.version)
                        if source_file.reference != parent:
                            raise ContractError(ErrorCode.STALE_CONTEXT)
                        refs.extend(source_file.dependencies)
                    dependencies = list(
                        {(r.source_id, r.version, r.scope.partition): r for r in refs}.values()
                    )
                    parents = list({(r.artifact_id, r.version): r for r in parents}.values())
                    if len(dependencies) > 200 or len(parents) > 200:
                        raise ContractError(ErrorCode.RESOURCE_BUSY)
                    await self.guard.verify(admission, dependencies, parents, "private")
                    desired = self.default_access if previous is None else previous.access
                    access = {"api_transfer": False, "public_disclosure": False}
                    purposes: tuple[Literal["api_transfer", "public_disclosure"], ...] = (
                        "api_transfer",
                        "public_disclosure",
                    )
                    for purpose in purposes:
                        if getattr(desired, purpose):
                            try:
                                await self.guard.verify(admission, dependencies, parents, purpose)
                            except ContractError as error:
                                if error.error.code is not ErrorCode.ACCESS_DENIED:
                                    raise
                            else:
                                access[purpose] = True
                    files, versions = await self.usage(area)
                    if (
                        files + (previous is None) > self.limits.max_files
                        or versions + 1 > self.limits.max_versions
                    ):
                        raise ContractError(ErrorCode.RESOURCE_BUSY)
                    version = 1 if previous is None else previous.version + 1
                    now = self.clock()
                    entry = ManagedFile(
                        id=change.file_id,
                        scope=area,
                        version=version,
                        created_at=now if previous is None else previous.created_at,
                        updated_at=now,
                        reference=ArtifactRef(
                            artifact_id=change.file_id,
                            version=version,
                            scope=area,
                            checksum=change.checksum,
                            size_bytes=len(content),
                            media_type=change.media_type,
                        ),
                        area=change.area,
                        path=change.path,
                        title=change.title,
                        kind=change.kind,
                        provenance=change.provenance,
                        dependencies=dependencies,
                        derived_from=parents,
                        access=FileAccess(**access),
                        state="active",
                        produced_by_attempt=admission.attempt_id,
                        change_reason=change.change_reason,
                    )
                    try:
                        await self.blobs.put(
                            area, change.checksum, content, self.limits.max_scope_bytes
                        )
                    except OSError as error:
                        raise ContractError(
                            ErrorCode.RESOURCE_BUSY
                            if str(error) == "BLOB_QUOTA"
                            else ErrorCode.SERVICE_UNAVAILABLE
                        ) from None
                    await self.guard.verify(admission, dependencies, parents, "private")
                    await self.authorize(admission, "write", change.area)
                    await unit.put(
                        entry,
                        expected_version=change.expected_version,
                        access=self.reader_access if previous is None else None,
                    )
                    await unit.complete(
                        Receipt(
                            request_id=change.request_id,
                            scope=area,
                            status="completed",
                            resource_id=entry.id,
                            version=entry.version,
                        )
                    )
        await self.guard.verify(admission, dependencies, parents, "private")
        await self.authorize(admission, "write", change.area)
        return entry

    async def operator_write(
        self,
        scope: Scope,
        area: str,
        path: str,
        title: str,
        media_type: str,
        content: bytes,
        *,
        file_id: str,
        request_id: str,
    ) -> ManagedFile:
        """Trusted operator upload: the same catalogue transaction and quotas as agent writes.

        No WorkAdmission, no agent permission and no attempt: the composition boundary has
        already authenticated the operator. New operator files (and only operator files,
        never the person's own) may be replaced under the same path with a new version;
        nothing here grants the agent rights or starts it.
        """
        if area not in OPERATOR_AREAS:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if (
            not isinstance(content, bytes)
            or not content
            or not isinstance(title, str)
            or not 1 <= len(title) <= 512
            or not isinstance(media_type, str)
            or not 1 <= len(media_type) <= 128
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if len(content) > self.limits.max_file_bytes:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        try:
            logical_path(path)
        except ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        checksum = hashlib.sha256(content).hexdigest()
        area_scope = stable_scope(scope)
        command = MessageCommand(
            request_id=request_id,
            scope=area_scope,
            target=file_id,
            type="dialogue.message",
            arguments=MessageArguments(text="workspace-file:operator:" + checksum[:32]),
        )
        async with self._writes:
            async with self.storage.transaction(self.principal, area_scope, command) as unit:
                if unit.replay is not None:
                    if unit.replay.resource_id != file_id or unit.replay.version is None:
                        raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
                    return await self._file(area_scope, file_id, unit.replay.version)
                previous = await self.storage.repository(
                    ManagedFile, self.principal, area_scope
                ).get(file_id)
                if previous is not None and (
                    previous.produced_by_attempt is not None or previous.state != "active"
                ):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if previous is not None and (
                    previous.area != area or previous.path != path
                ):
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
                dependencies = list(previous.dependencies) if previous else []
                parents = list(previous.derived_from) if previous else []
                files, versions = await self.usage(area_scope)
                if files + (previous is None) > self.limits.max_files or (
                    versions + 1 > self.limits.max_versions
                ):
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                version = 1 if previous is None else previous.version + 1
                now = self.clock()
                entry = ManagedFile(
                    id=file_id,
                    scope=area_scope,
                    version=version,
                    created_at=now if previous is None else previous.created_at,
                    updated_at=now,
                    reference=ArtifactRef(
                        artifact_id=file_id,
                        version=version,
                        scope=area_scope,
                        checksum=checksum,
                        size_bytes=len(content),
                        media_type=media_type,
                    ),
                    area=area,
                    path=path,
                    title=title,
                    kind="material",
                    provenance="read",
                    dependencies=dependencies,
                    derived_from=parents,
                    access=FileAccess(api_transfer=False, public_disclosure=False),
                    state="active",
                    produced_by_attempt=None,
                    change_reason="operator upload",
                )
                try:
                    await self.blobs.put(
                        area_scope, checksum, content, self.limits.max_scope_bytes
                    )
                except OSError as error:
                    raise ContractError(
                        ErrorCode.RESOURCE_BUSY
                        if str(error) == "BLOB_QUOTA"
                        else ErrorCode.SERVICE_UNAVAILABLE
                    ) from None
                await unit.put(
                    entry,
                    expected_version=None if previous is None else previous.version,
                    access=self.reader_access if previous is None else None,
                )
                await unit.complete(
                    Receipt(
                        request_id=request_id,
                        scope=area_scope,
                        status="completed",
                        resource_id=entry.id,
                        version=entry.version,
                    )
                )
                return entry

    async def operator_materials(
        self, scope: Scope, *, days: int = 7, limit: int = 10
    ) -> list[ManagedFile]:
        """Recent operator uploads for the person's next turn; metadata only, no content."""
        if not 1 <= days <= 366 or not 1 <= limit <= 50:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        since = self.clock() - timedelta(days=days)
        rows = [
            row
            for row in await self._entries(scope)
            if row.produced_by_attempt is None
            and row.state == "active"
            and row.updated_at >= since
        ]
        rows.sort(key=lambda row: (row.updated_at, row.id), reverse=True)
        return rows[:limit]
