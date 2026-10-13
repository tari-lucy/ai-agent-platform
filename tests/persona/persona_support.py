"""Shared fixtures for persona tests: real isolated SQLite, invented IDs and text only."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_application.persona import PersonaService, register_persona_extensions
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import (
    CreateCommand,
    EditCommand,
    MessageArguments,
    MessageCommand,
    Receipt,
)
from ai_agent_contracts.codec import from_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import Source, Topic
from pydantic import TypeAdapter
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
REFERENCE = (Path(__file__).parent / "fixtures" / "reference_persona.md").read_text("utf-8")

PERSONA = Principal("owner-persona", "core_owner", Owner.PERSONA)
MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
SOCIAL = Principal("owner-social", "core_owner", Owner.SOCIAL)
OPERATOR = Principal("operator-synthetic", "operator")
WORKER = Principal("worker-synthetic", "worker")


def run(coroutine):
    return asyncio.run(coroutine)


def make_scope(persona="persona-a", mode="work", space=None, workspace="synthetic-workspace"):
    return Scope(
        workspace_id=workspace,
        persona_id=persona,
        memory_space_id=space or "space-" + persona + "-" + mode,
        memory_mode=mode,
    )


GRANTS = (
    Grant(PERSONA, Owner.PERSONA, read=True, write=True),
    Grant(PERSONA, Owner.MEMORY, read=True),
    Grant(OPERATOR, Owner.PERSONA, read=True),
    Grant(MEMORY, Owner.MEMORY, read=True, write=True),
    Grant(SOCIAL, Owner.SOCIAL, read=True, write=True),
    Grant(MEMORY, Owner.PERSONA, read=True),
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def registry() -> ExtensionRegistry:
    found = ExtensionRegistry()
    register_persona_extensions(found)
    return found


class World:
    def __init__(self, store: SQLiteStore, service: PersonaService, extensions, clock: Clock):
        self.store, self.service, self.extensions, self.clock = store, service, extensions, clock

    async def provision(self, scope: Scope, grants=GRANTS) -> None:
        await self.store.provision(scope, grants)

    def command(self, model, type_, scope, request_id, schema_id, data, **extra):
        payload = {
            "request_id": request_id,
            "scope": scope.model_dump(mode="json"),
            "target": scope.persona_id,
            "type": type_,
            "arguments": {"schema_id": schema_id, "schema_version": "1.0", "data": data},
            **extra,
        }
        return from_wire(
            TypeAdapter(model), json.dumps(payload).encode(), extensions=self.extensions
        )

    def create(self, scope, markdown=REFERENCE, request_id="create-1") -> CreateCommand:
        return self.command(
            CreateCommand,
            "persona.create",
            scope,
            request_id,
            "persona.create-args",
            {"markdown": markdown},
        )

    def validate(self, scope, markdown, expected_version, request_id="validate-1") -> EditCommand:
        return self.command(
            EditCommand,
            "persona.validate",
            scope,
            request_id,
            "persona.validate-args",
            {"markdown": markdown},
            expected_version=expected_version,
        )

    def apply(
        self, scope, markdown, expected_version, request_id="apply-1", digest=None
    ) -> EditCommand:
        from ai_agent_application.persona import import_markdown

        if digest is None:
            parsed, _ = import_markdown(markdown)
            digest = parsed.digest if parsed else "0" * 64
        return self.command(
            EditCommand,
            "persona.apply",
            scope,
            request_id,
            "persona.apply-args",
            {"markdown": markdown, "revision_digest": digest},
            expected_version=expected_version,
        )

    def copy(self, scope, source_scope, source_version=1, request_id="copy-1") -> CreateCommand:
        return self.command(
            CreateCommand,
            "persona.copy",
            scope,
            request_id,
            "persona.copy-args",
            {
                "source_scope": source_scope.model_dump(mode="json"),
                "source_version": source_version,
            },
        )

    async def counts(self, scope: Scope | None = None) -> dict[str, int]:
        async with self.store.connection() as connection:
            result = {}
            for table in (db.records, db.revisions, db.operations, db.outbox):
                query = select(func.count()).select_from(table)
                if scope is not None:
                    query = query.where(table.c.scope_id == scope_key(scope))
                result[table.name] = await connection.scalar(query)
            return result

    async def memory_records(self, scope: Scope) -> int:
        async with self.store.connection() as connection:
            return int(
                await connection.scalar(
                    select(func.count())
                    .select_from(db.records)
                    .where(
                        db.records.c.scope_id == scope_key(scope), db.records.c.owner != "persona"
                    )
                )
                or 0
            )


async def new_world(tmp_path, scopes=(), *, reader_access=(), extra_scopes=()) -> World:
    base = Path(os.environ.get("M2_01_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    extensions = registry()
    store = SQLiteStore(base / (str(uuid4()) + ".sqlite"), clock=clock, extensions=extensions)
    await store.upgrade(MIGRATIONS)
    service = PersonaService(store, PERSONA, extensions, clock=clock, reader_access=reader_access)
    world = World(store, service, extensions, clock)
    for scope in (*scopes, *extra_scopes):
        await world.provision(scope)
    return world


def _decode(model, value):
    return from_wire(TypeAdapter(model), json.dumps(value).encode())


async def write_memory(world: World, scope: Scope, request_id="memory-1") -> None:
    """Invented memory in a scope: one source (memory owner) and one topic (social owner)."""
    stamp = NOW.isoformat()
    base = dict(scope=scope.model_dump(mode="json"), created_at=stamp, updated_at=stamp, version=1)
    source = _decode(
        Source,
        dict(
            id="source-1",
            source_id="source-1",
            provenance="simulation" if scope.memory_mode == "test" else "observation",
            source_type="synthetic-text",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=stamp,
            registered_at=stamp,
            retention={
                "policy_id": "synthetic-policy",
                "expires_at": (NOW + timedelta(days=30)).isoformat(),
            },
            availability="available",
            content="Вымышленный исходный текст.",
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
            **base,
        ),
    )
    topic = _decode(
        Topic,
        dict(
            id="topic-1",
            topic_id="topic-1",
            participant_ids=["synthetic-person"],
            sources=[],
            status="open",
            **base,
        ),
    )
    for principal, record, suffix in ((MEMORY, source, "a"), (SOCIAL, topic, "b")):
        command = MessageCommand(
            request_id=request_id + suffix,
            scope=scope,
            target="synthetic-target",
            type="dialogue.message",
            arguments=MessageArguments(text="Вымышленная команда."),
        )
        async with world.store.transaction(principal, scope, command) as unit:
            await unit.put(record)
            await unit.complete(
                Receipt(
                    request_id=command.request_id,
                    scope=scope,
                    status="completed",
                    resource_id=record.id,
                    version=1,
                )
            )


async def experience(world: World, scope: Scope, **changes: float | str) -> None:
    """Simulate accumulated live state: a later state version written by the persona owner."""
    from ai_agent_application.persona.schemas import ValueMap

    state = await world.service.get_state(scope)
    assert state is not None
    values = dict(world.service._state_values(state))
    values.update(changes)
    record = world.service._state_record(
        scope,
        ValueMap(values=values),
        state.version + 1,
        state.profile_version,
        ["change-1"],
        state.created_at,
        world.clock(),
    )
    command = MessageCommand(
        request_id="experience-" + str(state.version + 1),
        scope=scope,
        target="synthetic-target",
        type="dialogue.message",
        arguments=MessageArguments(text="Вымышленный опыт."),
    )
    async with world.store.transaction(PERSONA, scope, command) as unit:
        await unit.put(record, expected_version=state.version)
        await unit.complete(
            Receipt(
                request_id=command.request_id,
                scope=scope,
                status="completed",
                resource_id=record.id,
                version=record.version,
            )
        )


__all__ = ["RecordAccess"]
