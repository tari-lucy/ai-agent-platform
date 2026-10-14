"""Reviewed settings transfer: current API rights, finite durable plan and owner journal."""

from datetime import timedelta
from typing import cast

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.policy import scope_key, stamp, visible
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory.transfer import Key, SettingsTransfer
from ai_agent_application.memory.service import derived_id
from ai_agent_application.persona import PersonaService
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import EditCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, VersionedRecord
from ai_agent_contracts.panel import (
    SettingsTransferArguments,
    SettingsTransferItem,
    SettingsTransferPreview,
)
from ai_agent_contracts.persona import (
    CharacterState,
    GmRuntimeState,
    Profile,
    SettingsTransferOperation,
)
from ai_agent_contracts.states import ErrorCode, RunState
from pydantic import JsonValue
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from .service import Runtime
from .settings import DurableSettings, TransferJournal
from .state import PRINCIPALS


class Access:
    def __init__(self, rt: Runtime, authority: ControlAuthority, request: EditCommand):
        self.rt, self.authority, self.request = rt, authority, request
        self.pins: list[tuple[VersionedRecord, bool]] = []

    async def read[T: VersionedRecord](
        self, model: type[T], scope: Scope, identifier: str, *, source: bool = False
    ) -> T:
        record = await self.rt.store.repository(
            model, Principal(self.authority.principal_id, "operator"), scope
        ).get(identifier, purpose="api_transfer")
        if record is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.pins.append((record, source))
        return record

    async def load(self, args: SettingsTransferArguments) -> GmRuntimeState:
        rt, scope = self.rt, self.request.scope
        if (
            scope.memory_mode != "work"
            or args.source_scope.memory_mode != "test"
            or args.source_scope.workspace_id != scope.workspace_id
            or args.source_scope.partition not in self.authority.partitions
            or args.source_scope.partition not in {s.partition for s in rt.scopes}
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        target = await self.read(GmRuntimeState, scope, "gm-runtime")
        if any(k != "profile" for k in args.keys):
            await self.read(GmRuntimeState, args.source_scope, "gm-runtime", source=True)
        if "profile" in args.keys:
            await self.read(Profile, args.source_scope, args.source_scope.persona_id, source=True)
            await self.read(Profile, scope, scope.persona_id)
            await self.read(CharacterState, scope, "state:" + scope.persona_id)
        return target

    async def check(self, connection: AsyncConnection, *, pin_target_profile: bool = False) -> None:
        self.rt.authorize_configuration(self.authority, self.request.scope)
        principal = Principal(self.authority.principal_id, "operator")
        for record, source in self.pins:
            row = db.records
            pin = source or pin_target_profile and isinstance(record, (Profile, CharacterState))
            found = await connection.scalar(
                select(row.c.id).where(
                    row.c.scope_id == scope_key(record.scope),
                    row.c.kind == type(record).__name__,
                    row.c.id == record.id,
                    visible(
                        row, row.c.version, principal, "api_transfer", stamp(self.rt.store.clock())
                    ),
                    row.c.version == record.version if pin else true(),
                )
            )
            if found is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)


def arguments(rt: Runtime, request: EditCommand) -> SettingsTransferArguments:
    if request.type != "laboratory.transfer_settings" or request.target != "laboratory":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    if request.arguments.schema_id != "laboratory.transfer-settings-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if not isinstance(args, SettingsTransferArguments):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    return args


def service(rt: Runtime) -> tuple[SettingsTransfer, DurableSettings]:
    holder = DurableSettings(rt.state)
    persona = PersonaService(rt.store, PRINCIPALS[Owner.PERSONA], rt.registry)
    return SettingsTransfer(persona, holder), holder


async def preview(
    rt: Runtime, authority: ControlAuthority, request: EditCommand
) -> SettingsTransferPreview:
    rt.authorize_configuration(authority, request.scope)
    args = arguments(rt, request)
    if args.action != "preview":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    access = Access(rt, authority, request)
    target = await access.load(args)
    if request.expected_version != target.version:
        raise ContractError(ErrorCode.VERSION_CONFLICT)
    transfer, holder = service(rt)
    reviewed = await transfer.preview(args.source_scope, request.scope, args.keys)
    blocked_pending = await TransferJournal(rt.state).pending(request.scope)
    items = []
    for item in reviewed.items:
        before, after = item.before, item.after
        if item.key == "profile":
            before = await transfer.persona.export_markdown(request.scope)
            after = await transfer.persona.export_markdown(args.source_scope)
        elif item.key == "story_rules_version":

            def rules(scope: Scope) -> dict[str, JsonValue]:
                record = holder.snapshots[(scope.partition, "story_rules_version")]
                return {"rules_version": record.rules_version, "rules": record.rules.data}

            before, after = rules(request.scope), rules(args.source_scope)
        blockers = list(item.blockers) + (
            ["TRANSFER_RECONCILIATION_REQUIRED"] if blocked_pending else []
        )
        items.append(
            SettingsTransferItem(
                key=cast(Key, item.key),
                before=before,
                after=after,
                changes=list(item.changes),
                blockers=blockers,
            )
        )
    now = rt.supervisor.clock.utc()
    plan = SettingsTransferPreview(
        id=derived_id("transfer-preview", request.scope, request.request_id),
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=5),
        request_id=request.request_id,
        operation_id=args.operation_id,
        operator_id=authority.principal_id,
        source_scope=args.source_scope,
        keys=args.keys,
        preview_digest=reviewed.digest,
        target_version=target.version,
        items=items,
        never_transferred=list(reviewed.never_transferred),
        blocked=any(i.blockers for i in items),
    )

    async def preview_guard(connection: AsyncConnection) -> None:
        await access.check(connection, pin_target_profile=True)

    with rt.store.authorize_writes(preview_guard, owners=frozenset({Owner.WORKSPACE})):
        async with rt.store.transaction(
            PRINCIPALS[Owner.WORKSPACE], request.scope, request
        ) as unit:
            if unit.replay is not None:
                previous = await rt.store.repository(
                    SettingsTransferPreview, PRINCIPALS[Owner.WORKSPACE], request.scope
                ).get(plan.id)
                if previous is None:
                    raise ContractError(ErrorCode.EXPIRED)
                if (
                    previous.preview_digest != plan.preview_digest
                    or previous.blocked != plan.blocked
                ):
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                return plan.model_copy(
                    update={
                        "created_at": previous.created_at,
                        "updated_at": previous.updated_at,
                        "expires_at": previous.expires_at,
                    }
                )
            proof = plan.model_copy(
                update={
                    "items": [
                        item.model_copy(update={"before": None, "after": None})
                        for item in plan.items
                    ]
                }
            )
            await unit.put(proof)
            await unit.complete(
                Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=plan.id,
                    version=1,
                )
            )
    return plan


async def apply(rt: Runtime, authority: ControlAuthority, request: EditCommand) -> Receipt:
    args = arguments(rt, request)
    if args.action == "preview":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    if rt.supervisor.snapshot.state != RunState.STOPPED:
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    journal = TransferJournal(rt.state, clock=rt.supervisor.clock.utc, readers=rt.readers)
    old = await journal.get(request.scope, args.operation_id)
    if old is not None and (
        old.operator_id != authority.principal_id
        or old.preview_digest != args.preview_digest
        or old.source_scope.partition != args.source_scope.partition
        or old.selected_keys != args.keys
    ):
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
    if old is not None:
        allowed = await rt.store.repository(
            SettingsTransferOperation, Principal(authority.principal_id, "operator"), request.scope
        ).get(old.id, purpose="api_transfer")
        if allowed is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
    if args.source_scope.partition not in authority.partitions:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if args.action == "acknowledge":
        if old is None or request.expected_version != old.version:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        await journal.acknowledge(request.scope, args.operation_id, authority.principal_id)
    elif old is not None:
        if old.status != "completed":
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
    else:
        access = Access(rt, authority, request)
        target = await access.load(args)
        if request.expected_version != target.version:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        plan = await rt.store.repository(
            SettingsTransferPreview, PRINCIPALS[Owner.WORKSPACE], request.scope
        ).get(cast(str, args.plan_id))
        if plan is None or rt.supervisor.clock.utc() >= plan.expires_at:
            raise ContractError(ErrorCode.EXPIRED)
        if (
            plan.operator_id != authority.principal_id
            or plan.version != args.plan_version
            or plan.operation_id != args.operation_id
            or plan.preview_digest != args.preview_digest
            or plan.keys != args.keys
            or plan.source_scope.partition != args.source_scope.partition
            or plan.blocked
            or plan.target_version != request.expected_version
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if await journal.pending(request.scope):
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        transfer, _ = service(rt)

        def stopped() -> None:
            rt.authorize_configuration(authority, request.scope)
            if rt.supervisor.snapshot.state != RunState.STOPPED:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            if rt.supervisor.clock.utc() >= plan.expires_at:
                raise ContractError(ErrorCode.EXPIRED)

        checked_connections: set[AsyncConnection] = set()

        async def persona_guard(connection: AsyncConnection) -> None:
            # Pin once on transaction entry; later checks see this owner's own writes.
            await access.check(connection, pin_target_profile=connection not in checked_connections)
            checked_connections.add(connection)

        with rt.store.fence(stopped, owner=Owner.PERSONA), rt.store.fence(stopped, owner=Owner.GM):
            with (
                rt.store.authorize_writes(persona_guard, owners=frozenset({Owner.PERSONA})),
                rt.store.authorize_writes(access.check, owners=frozenset({Owner.GM})),
            ):
                await journal.apply(
                    transfer,
                    args.source_scope,
                    request.scope,
                    args.keys,
                    args.operation_id,
                    args.preview_digest,
                    authority.principal_id,
                )
    current = await journal.get(request.scope, args.operation_id)
    assert current is not None
    return Receipt(
        request_id=request.request_id,
        scope=request.scope,
        status="completed",
        resource_id=current.id,
        version=current.version,
    )
