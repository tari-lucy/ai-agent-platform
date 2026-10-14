"""Existing owner transactions and CAS; this module never provisions permissions."""

import hashlib
from datetime import UTC, datetime
from typing import Any

from ai_agent_application.gamemaster.rules import StoryRule, StoryRules
from ai_agent_application.llm import ResolvedAssignment, resolve_assignment
from ai_agent_application.storage import Owner, Principal, RecordAccess, Storage
from ai_agent_contracts.api import (
    EditCommand,
    MessageArguments,
    MessageCommand,
    Receipt,
)
from ai_agent_contracts.codec import ContractError, to_wire
from ai_agent_contracts.common import ExtensionPayload, Record, Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import Connection, LLMAssignment
from ai_agent_contracts.persona import GmRuntimeState
from ai_agent_contracts.states import ErrorCode
from pydantic import BaseModel, JsonValue, TypeAdapter

from .schemas import ConfigureGM, RulesData

PRINCIPALS = {owner: Principal("core-" + owner.value, "core_owner", owner) for owner in Owner}
OPERATOR = Principal("operator", "operator")
GM_STATE_ID = "gm-runtime"
ROLES = {
    "extract": ("extractor",),
    "consolidate": ("consolidator",),
    "reflect": ("reflection", "story-screen"),
    "post_session": ("extractor", "consolidator", "reflection", "story-screen"),
    "gm": ("game-master", "story-screen"),
    "laboratory": (),
}
CAPABILITIES = tuple("m3." + kind for kind in ROLES)


def record[T: BaseModel](registry: ExtensionRegistry, model: type[T], **payload: Any) -> T:
    return model.model_validate(payload, context={"extensions": registry})


def extension(registry: ExtensionRegistry, name: str, data: JsonValue) -> ExtensionPayload:
    return TypeAdapter(ExtensionPayload).validate_python(
        {"schema_id": name, "schema_version": "1.0", "data": data},
        context={"extensions": registry},
    )


def command(scope: Scope, name: str, text: str = "owner-write") -> MessageCommand:
    return MessageCommand(
        request_id=name,
        scope=scope,
        target="m3",
        type="dialogue.message",
        arguments=MessageArguments(text=text),
    )


async def put(
    store: Storage,
    registry: ExtensionRegistry,
    owner: Owner,
    scope: Scope,
    name: str,
    record: Record,
    expected: int | None,
    access: tuple[RecordAccess, ...] = (),
) -> Receipt:
    digest = hashlib.sha256(to_wire(record, extensions=registry)).hexdigest()
    request = command(scope, name, digest)
    async with store.transaction(PRINCIPALS[owner], scope, request) as unit:
        if unit.replay is not None:
            return unit.replay
        await unit.put(record, expected_version=expected, access=access or None)
        receipt = Receipt(
            request_id=name,
            scope=scope,
            status="completed",
            resource_id=record.id,
            version=getattr(record, "version", 1),
        )
        await unit.complete(receipt)
        return receipt


def rules_of(record: GmRuntimeState, registry: ExtensionRegistry) -> StoryRules:
    payload = registry.validate(
        record.rules.schema_id, record.rules.schema_version, record.rules.data
    )
    if not isinstance(payload, RulesData):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    layers = {
        level: tuple(
            StoryRule(r.rule_id, r.level, r.kind, r.description, tuple(r.markers))
            for r in payload.rules
            if r.level == level
        )
        for level in ("project", "profile", "session")
    }
    return StoryRules.layered(
        record.rules_version, layers["project"], layers["profile"], layers["session"]
    )


class State:
    def __init__(self, storage: Storage, registry: ExtensionRegistry):
        self.storage, self.registry = storage, registry

    async def gm(self, scope: Scope) -> GmRuntimeState:
        record = await self.storage.repository(GmRuntimeState, PRINCIPALS[Owner.GM], scope).get(
            GM_STATE_ID
        )
        if record is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        rules_of(record, self.registry)
        return record

    async def checkpoint_time(self, scope: Scope, seconds: float) -> None:
        old = await self.gm(scope)
        if seconds <= old.active_seconds:
            return
        record = old.model_copy(
            update={
                "active_seconds": seconds,
                "scope": scope,
                "version": old.version + 1,
                "updated_at": max(datetime.now(UTC), old.updated_at),
            }
        )
        await put(
            self.storage,
            self.registry,
            Owner.GM,
            scope,
            f"gm-clock-{record.version}",
            record,
            old.version,
        )

    async def roles(self, scope: Scope) -> dict[str, LLMAssignment]:
        repo = self.storage.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope)
        rows = await repo.list(limit=100)
        if len(rows) == 100 or len({r.role_id for r in rows}) != len(rows):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        return {row.role_id: row for row in rows}

    async def binding(self, scope: Scope, assignment: LLMAssignment) -> ResolvedAssignment:
        connection = await self.storage.repository(
            Connection, PRINCIPALS[Owner.WORKSPACE], scope
        ).get(assignment.connection_id)
        if connection is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        return resolve_assignment(assignment, connection, self.registry)

    async def configure(self, request: EditCommand, args: ConfigureGM) -> Receipt:
        async with self.storage.transaction(PRINCIPALS[Owner.GM], request.scope, request) as unit:
            if unit.replay is not None:
                return unit.replay
            old = await self.gm(request.scope)
            if request.expected_version != old.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            values = old.model_dump()
            values["scope"] = request.scope.model_dump()
            if args.intensity is not None:
                values["intensity"] = args.intensity
            if args.rules is not None:
                if args.rules_version is None or args.rules_version <= old.rules_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                values.update(
                    rules_version=args.rules_version,
                    rules=extension(self.registry, "m3.rules", args.rules.model_dump(mode="json")),
                )
            elif args.rules_version is not None and args.rules_version != old.rules_version:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            values.update(
                version=old.version + 1, updated_at=max(datetime.now(UTC), old.updated_at)
            )
            record = GmRuntimeState.model_validate(values, context={"extensions": self.registry})
            rules_of(record, self.registry)
            await unit.put(record, expected_version=old.version)
            receipt = Receipt(
                request_id=request.request_id,
                scope=request.scope,
                status="completed",
                resource_id=record.id,
                version=record.version,
            )
            await unit.complete(receipt)
            return receipt
