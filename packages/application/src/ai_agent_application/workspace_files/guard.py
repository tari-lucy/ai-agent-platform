"""One source/file release check reusable by workplace owners and their gateways."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ai_agent_contracts.agent_workplace import ManagedFile
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_application.retention.ports import DeletionLedger
from ai_agent_application.storage import Principal, ReadPurpose, Storage


def stable_scope(scope: Scope) -> Scope:
    return Scope(
        workspace_id=scope.workspace_id,
        persona_id=scope.persona_id,
        memory_space_id=scope.memory_space_id,
        memory_mode=scope.memory_mode,
    )


def current(admission: WorkAdmission) -> None:
    if (
        not admission.is_current()
        or admission.lease_version < 1
        or admission.scope.run_id != admission.freshness.run_id
        or not admission.attempt_id
        or not admission.operation_id
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)


class FileReleaseGuard:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        ledger: DeletionLedger,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.storage, self.principal, self.ledger, self.clock = storage, principal, ledger, clock

    async def check(
        self,
        scope: Scope,
        sources: Sequence[SourceRef],
        files: Sequence[ArtifactRef],
        purpose: ReadPurpose = "private",
    ) -> None:
        if purpose not in {"private", "api_transfer", "public_disclosure"}:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        area = stable_scope(scope)
        pending = list(files)
        refs = {(ref.source_id, ref.version, ref.scope.partition): ref for ref in sources}
        seen: set[tuple[str, int]] = set()
        while pending:
            ref = pending.pop()
            if ref.scope.partition != area.partition:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            key = (ref.artifact_id, ref.version)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 200 or len(refs) > 200:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            repo = self.storage.repository(ManagedFile, self.principal, area)
            latest = await repo.get(ref.artifact_id)
            selected = (
                latest
                if latest is not None and latest.version == ref.version
                else await repo.get(ref.artifact_id, version=ref.version)
            )
            for entry in (latest, selected):
                if entry is None or entry.state != "active":
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if entry.expires_at is not None and entry.expires_at <= self.clock():
                    raise ContractError(ErrorCode.EXPIRED)
                if (
                    purpose == "api_transfer"
                    and not entry.access.api_transfer
                    or purpose == "public_disclosure"
                    and not entry.access.public_disclosure
                ):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
            assert selected is not None
            if selected.reference != ref:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            refs.update(
                {
                    (ref.source_id, ref.version, ref.scope.partition): ref
                    for ref in selected.dependencies
                }
            )
            pending.extend(selected.derived_from)
        if len(refs) > 200:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        blocked = {entry.source_id for entry in await self.ledger.entries(area)}
        for source_ref in refs.values():
            if (
                source_ref.scope.partition != area.partition
                or source_ref.availability != "available"
                or source_ref.source_id in blocked
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)
        source_rows = (
            await self.storage.repository(Source, self.principal, area).get_many(
                [ref.source_id for ref in refs.values()], purpose=purpose
            )
            if refs
            else []
        )
        found = {row.id: row for row in source_rows}
        for source_ref in refs.values():
            source = found.get(source_ref.source_id)
            if source is None or source.availability != "available":
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if source.version != source_ref.version:
                raise ContractError(ErrorCode.STALE_CONTEXT)

    async def verify(
        self,
        admission: WorkAdmission,
        sources: Sequence[SourceRef],
        files: Sequence[ArtifactRef],
        purpose: ReadPurpose,
    ) -> None:
        current(admission)
        await self.check(admission.scope, sources, files, purpose)
        current(admission)
