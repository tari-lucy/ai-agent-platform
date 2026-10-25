"""Explicit bounded source plans over retention; no raw export archive is kept."""

import hashlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import derived_id
from ai_agent_application.retention import DeletionPreview, RetentionService
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.api import (
    ConsequencePlan,
    ExecutePlanCommand,
    PlanCommand,
    PlanDependency,
    Receipt,
)
from ai_agent_contracts.codec import ContractError, to_wire
from ai_agent_contracts.common import ArtifactRef, Scope, VersionedRecord
from ai_agent_contracts.memory import Source
from ai_agent_contracts.panel import (
    MemoryPlanProof,
    MemorySelectionArguments,
    MemorySourceExport,
    OwnerOperation,
)
from ai_agent_contracts.states import ErrorCode, RunState
from sqlalchemy import select, tuple_

from .development_controls import Pins
from .owner_operations import OwnerOperations
from .state import PRINCIPALS, put

if TYPE_CHECKING:
    from .service import Runtime


def retention(rt: Runtime) -> RetentionService:
    if rt.retention is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    return rt.retention


def guard(rt: Runtime, authority: ControlAuthority, scope: Scope, deleting: bool) -> None:
    rt.authorize_configuration(authority, scope)
    if deleting and rt.supervisor.snapshot.state is not RunState.STOPPED:
        raise ContractError(ErrorCode.RESOURCE_BUSY)


async def source(
    rt: Runtime,
    authority: ControlAuthority,
    request: PlanCommand | ExecutePlanCommand,
    expected: int,
) -> Source:
    row = await Pins(rt, authority, request, Owner.WORKSPACE).read(Source, request.target)
    guard(rt, authority, request.scope, request.type.startswith("memory.delete"))
    if row.version != expected:
        raise ContractError(ErrorCode.VERSION_CONFLICT, request.request_id, row.version)
    return row


def proof_sources(proof: MemoryPlanProof) -> list[tuple[str, int]]:
    return proof.source_versions or [(proof.source_id, proof.source_version)]


async def pin_session(pins: Pins, ref: tuple[str, int] | None) -> None:
    if ref is None:
        return
    row = await pins.read(WorkSession, ref[0])
    if row.version != ref[1] or row.visibility == "active":
        raise ContractError(ErrorCode.VERSION_CONFLICT)


async def check_sources(
    rt: Runtime, authority: ControlAuthority, request: ExecutePlanCommand, proof: MemoryPlanProof
) -> None:
    pins = Pins(rt, authority, request, Owner.WORKSPACE)
    await pin_session(pins, proof.session_ref)
    for identifier, version in proof_sources(proof):
        row = await pins.read(Source, identifier)
        if row.version != version:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
    guard(rt, authority, request.scope, True)


def snapshot(proof: MemoryPlanProof) -> DeletionPreview:
    return DeletionPreview(
        tuple(proof.deletion_source_ids or [identifier for identifier, _ in proof_sources(proof)]),
        tuple(proof.erased),
        tuple(proof.detached),
        tuple(proof.indexed),
        proof.digest,
    )


async def dependency_digest(rt: Runtime, scope: Scope, region: DeletionPreview) -> str:
    """Retention selected these identities. Read version metadata only, never text."""
    identities = tuple(sorted(set((*region.erased_with, *region.detached))))
    if not identities:
        return hashlib.sha256(b"[]").hexdigest()
    async with rt.store.connection() as connection:
        rows = (
            await connection.execute(
                select(
                    db.records.c.kind,
                    db.records.c.id,
                    db.records.c.version,
                    db.records.c.deleted,
                    db.records.c.available,
                    db.records.c.expires_at,
                )
                .where(
                    db.records.c.scope_id == scope_key(scope),
                    tuple_(db.records.c.kind, db.records.c.id).in_(identities),
                )
                .order_by(db.records.c.kind, db.records.c.id)
            )
        ).all()
    if len(rows) != len(identities):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    return hashlib.sha256(json.dumps([list(row) for row in rows]).encode()).hexdigest()


async def preview(rt: Runtime, authority: ControlAuthority, request: PlanCommand) -> Receipt:
    operation = request.type.removesuffix(".preview")
    deleting = operation == "memory.delete"
    if operation not in ("memory.delete", "memory.export") or (
        request.arguments.schema_id != operation + "-args"
    ):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if (
        not isinstance(args, MemorySelectionArguments)
        or args.source_ids[0] != request.target
        or len(set(args.source_ids)) != len(args.source_ids)
        or (not deleting and args.source_ids != [request.target])
    ):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    guard(rt, authority, request.scope, deleting)
    pins = Pins(rt, authority, request, Owner.WORKSPACE)
    await pin_session(pins, args.session_ref)
    row = await pins.read(Source, request.target)
    if row.version != request.expected_version:
        raise ContractError(ErrorCode.VERSION_CONFLICT, request.request_id, row.version)
    selected = [row]
    for identifier in args.source_ids[1:]:
        selected.append(await pins.read(Source, identifier))
    region = (
        await retention(rt).preview_delete(request.scope, args.source_ids) if deleting else None
    )
    if region and len(region.source_ids) > 200:
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    if region:
        known_ids = {item.id for item in selected}
        for identifier in region.source_ids:
            if identifier not in known_ids:
                selected.append(await pins.read(Source, identifier))
    if region and len(region.erased_with) + len(region.detached) > 200:
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    now = rt.supervisor.clock.utc()
    identifier = derived_id("memory-plan", request.scope, request.request_id)
    proof = MemoryPlanProof(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operator_id=authority.principal_id,
        operation_type="memory.delete" if deleting else "memory.export",
        source_id=row.id,
        source_version=row.version,
        source_versions=[(item.id, item.version) for item in selected] if deleting else [],
        selected_source_ids=args.source_ids if deleting else [],
        session_ref=args.session_ref if deleting else None,
        deletion_source_ids=list(region.source_ids) if region else [],
        digest=region.digest if region else hashlib.sha256(to_wire(row)).hexdigest(),
        dependency_digest=await dependency_digest(rt, request.scope, region)
        if region
        else hashlib.sha256(b"[]").hexdigest(),
        erased=list(region.erased_with) if region else [],
        detached=list(region.detached) if region else [],
        indexed=list(region.indexed) if region else [],
        plan_expires_at=now + timedelta(minutes=5),
        expires_at=now + rt.store.receipt_retention,
    )
    consequences = (
        f"Удалить выбранные источники ({len(selected)}) и все их редакции. "
        f"Зависимых записей без другого основания: {len(proof.erased)}; "
        f"с другими основаниями: {len(proof.detached)}. Очистить индексы, кэши и "
        "зависимые тела лабораторных отчётов. Старые резервные копии закрыты журналом "
        "удалений при восстановлении; внешние скачанные копии удаляются отдельно. "
        "При сбое источник останется заблокирован, а очистка — незавершённой."
        if deleting
        else f"Скачать JSON выбранного источника версии {row.version}: его текст и метаданные. "
        "Другие источники, диалоги, производные записи и файлы медиа не включаются. "
        "Перед скачиванием права и версия проверяются снова. Сервер не хранит копию экспорта; "
        "уже скачанный файл остаётся у оператора."
    )
    plan = ConsequencePlan(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operation_type=operation,
        consequences=consequences,
        dependencies=[
            PlanDependency(resource_id=item.id, version=item.version) for item in selected
        ],
        expires_at=proof.plan_expires_at,
    )
    with (
        rt.store.fence(
            lambda: guard(rt, authority, request.scope, deleting), owner=Owner.WORKSPACE
        ),
        rt.store.authorize_writes(pins.check, owners=frozenset({Owner.WORKSPACE})),
    ):
        async with rt.store.transaction(
            PRINCIPALS[Owner.WORKSPACE], request.scope, request
        ) as unit:
            if unit.replay is not None:
                return unit.replay
            await unit.put(plan, access=rt.readers)
            await unit.put(proof)
            receipt = Receipt(
                request_id=request.request_id,
                scope=request.scope,
                status="completed",
                resource_id=plan.id,
                version=1,
            )
            await unit.complete(receipt)
            return receipt


async def execute(rt: Runtime, authority: ControlAuthority, request: ExecutePlanCommand) -> Receipt:
    operation = request.type.removesuffix(".execute")
    deleting = operation == "memory.delete"
    guard(rt, authority, request.scope, deleting)
    ops = OwnerOperations(rt)
    known = await ops.known(authority, request)
    if known is not None and known.receipt is not None:
        return known.receipt
    loaded = await rt.store.repository(
        MemoryPlanProof, PRINCIPALS[Owner.WORKSPACE], request.scope
    ).get(request.arguments.plan_id)
    if loaded is None:
        raise ContractError(ErrorCode.EXPIRED)
    proof: MemoryPlanProof = loaded
    if (
        proof.operator_id != authority.principal_id
        or proof.operation_type != operation
        or proof.source_id != request.target
        or request.arguments.plan_version != 1
        or request.expected_version != 1
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    op_id = derived_id("owner-op", request.scope, request.request_id)
    if proof.execution_id not in (None, op_id):
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
    if known is None:
        if rt.supervisor.clock.utc() >= proof.plan_expires_at:
            raise ContractError(ErrorCode.EXPIRED)
        if deleting:
            await check_sources(rt, authority, request, proof)
        else:
            await source(rt, authority, request, proof.source_version)
        if deleting and await retention(rt).preview_delete(
            request.scope, proof.selected_source_ids or [proof.source_id]
        ) != snapshot(proof):
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        if (
            deleting
            and await dependency_digest(rt, request.scope, snapshot(proof))
            != proof.dependency_digest
        ):
            raise ContractError(ErrorCode.VERSION_CONFLICT)

    async def recover(identifier: str) -> list[VersionedRecord] | None:
        if proof.execution_id != identifier:
            return None
        if not deleting:
            return [proof]
        service = retention(rt)
        entries = {entry.source_id: entry for entry in await service.ledger.entries(request.scope)}
        selected = proof_sources(proof)
        missing = []
        for source_id, version in selected:
            entry = entries.get(source_id)
            if entry is None:
                missing.append(source_id)
            elif (
                entry.reason != "operator_delete"
                or entry.version != version
                or known is None
                or entry.blocked_at < known.created_at
            ):
                return None
        if missing:
            # Atomic batches publish all or none. A legacy partial region can only
            # resume here if every pinned source and the exact region still survive.
            await check_sources(rt, authority, request, proof)
            if (
                await service.preview_delete(
                    request.scope, proof.selected_source_ids or [proof.source_id]
                )
                != snapshot(proof)
                or await dependency_digest(rt, request.scope, snapshot(proof))
                != proof.dependency_digest
            ):
                return None

            async def before_remaining() -> None:
                await check_sources(rt, authority, request, proof)
                if (
                    await dependency_digest(rt, request.scope, snapshot(proof))
                    != proof.dependency_digest
                ):
                    raise ContractError(ErrorCode.VERSION_CONFLICT)

            result = await service.delete(
                request.scope, snapshot(proof), before_block=before_remaining
            )
            if not result.complete:
                return None
        elif any(entries[source_id].purged_at is None for source_id, _ in selected):
            await service.resume(request.scope, source_ids=tuple(i for i, _ in selected))
        entries = {entry.source_id: entry for entry in await service.ledger.entries(request.scope)}
        if all(entries[i].purged_at is not None for i, _ in selected):
            await pin_session(Pins(rt, authority, request, Owner.WORKSPACE), proof.session_ref)
            return [proof]
        return None

    pending: tuple[list[str], list[str]] = ([], [])

    async def perform(identifier: str) -> list[VersionedRecord] | None:
        nonlocal proof
        nonlocal pending
        pins = Pins(rt, authority, request, Owner.WORKSPACE)
        await pin_session(pins, proof.session_ref)
        row = await pins.read(Source, proof.source_id)
        if row.version != proof.source_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        if deleting:
            for source_id, version in proof_sources(proof):
                other = await pins.read(Source, source_id)
                if other.version != version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
        updated = proof.model_copy(
            update={
                "version": 2,
                "execution_id": identifier,
                "updated_at": rt.supervisor.clock.utc(),
            }
        )
        with (
            rt.store.fence(
                lambda: guard(rt, authority, request.scope, deleting), owner=Owner.WORKSPACE
            ),
            rt.store.authorize_writes(pins.check, owners=frozenset({Owner.WORKSPACE})),
        ):
            await put(
                rt.store,
                rt.registry,
                Owner.WORKSPACE,
                proof.scope,
                "memory-plan-claim-" + identifier,
                updated,
                1,
            )
        proof = updated
        if deleting:

            async def before_block() -> None:
                await check_sources(rt, authority, request, proof)
                if (
                    await dependency_digest(rt, request.scope, snapshot(proof))
                    != proof.dependency_digest
                ):
                    raise ContractError(ErrorCode.VERSION_CONFLICT)

            result = await retention(rt).delete(
                request.scope, snapshot(proof), before_block=before_block
            )
            if not result.complete:
                blocked = all(o.failed_step in ("derived", "physical") for o in result.outcomes)
                pending = (
                    ["Источник заблокирован"] if blocked else [],
                    ["Очистка источника, индексов и зависимых копий"],
                )
                return None
        await pin_session(Pins(rt, authority, request, Owner.WORKSPACE), proof.session_ref)
        return [proof]

    with rt.store.fence(
        lambda: guard(rt, authority, request.scope, deleting), owner=Owner.WORKSPACE
    ):
        return await ops.execute(
            authority,
            request,
            "memory",
            perform,
            recover,
            expected_result_ids=[proof.id],
            pending_steps=lambda: pending,
        )


async def download(
    rt: Runtime, principal_id: str, scope: Scope, identifier: str
) -> tuple[ArtifactRef, bytes]:
    principal = Principal(principal_id, "operator")
    operation = await rt.store.repository(OwnerOperation, principal, scope).get(
        identifier, purpose="api_transfer"
    )
    if (
        operation is None
        or operation.operator_id != principal_id
        or operation.command_type != "memory.export.execute"
        or operation.state != "completed"
        or len(operation.expected_result_ids) != 1
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    proof = await rt.store.repository(MemoryPlanProof, PRINCIPALS[Owner.WORKSPACE], scope).get(
        operation.expected_result_ids[0]
    )
    if (
        proof is None
        or proof.execution_id != identifier
        or proof.operator_id != principal_id
        or proof.operation_type != "memory.export"
        or proof.source_id != operation.target_id
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    repository = rt.store.repository(Source, principal, scope)
    row = await repository.get(proof.source_id, purpose="api_transfer")
    if row is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if (
        row.version != proof.source_version
        or hashlib.sha256(to_wire(row)).hexdigest() != proof.digest
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    body = to_wire(MemorySourceExport(exported_at=operation.created_at, source=row))
    if await repository.get(row.id, purpose="api_transfer") != row:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    return ArtifactRef(
        artifact_id=identifier,
        version=1,
        scope=proof.scope,
        checksum=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        media_type="application/json",
    ), body
