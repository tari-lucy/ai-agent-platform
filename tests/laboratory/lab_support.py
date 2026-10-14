"""Fixtures for M3-04: real SQLite store, fresh isolated test scopes, a seeded work scope."""

import asyncio
import json
import os
from datetime import timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.laboratory import LabRunner, VirtualClock, lab_grants
from ai_agent_application.laboratory.lab import MEMORY, PERSONA
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.persona import PersonaService, register_persona_extensions
from ai_agent_application.social import register_social_extensions
from ai_agent_contracts.api import CreateCommand, EditCommand, MessageArguments, MessageCommand
from ai_agent_contracts.codec import from_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import Provenance
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
_ids = count(1)


def run(coroutine):
    return asyncio.run(coroutine)


def scope_of(mode: str, space: str, persona: str = "persona-lab") -> Scope:
    return Scope(
        workspace_id="synthetic-workspace",
        persona_id=persona,
        memory_space_id=space,
        memory_mode=mode,
    )


def registry() -> ExtensionRegistry:
    found = ExtensionRegistry()
    register_persona_extensions(found)
    register_social_extensions(found)
    from ai_agent_application.development import register_development_extensions

    register_development_extensions(found)
    return found


class Env:
    def __init__(self, store, work, runner, base) -> None:
        self.store, self.work, self.runner, self.base = store, work, runner, base


async def new_env(tmp_path, *, seed_work=True) -> Env:
    base = Path(os.environ.get("M3_04_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    reg = registry()
    clock = VirtualClock()
    store = SQLiteStore(base / f"{uuid4()}.sqlite", extensions=reg, clock=clock)
    await store.upgrade(MIGRATIONS)
    work = scope_of("work", "space-work")
    await store.provision(work, lab_grants())
    if seed_work:
        memory = MemoryService(store, MEMORY, reg, clock=clock)
        await memory.register_source(
            MessageCommand(
                request_id="seed-work-source",
                scope=work,
                target="t",
                type="dialogue.message",
                arguments=MessageArguments(text="x"),
            ),
            SourceDraft(
                provenance=Provenance.THIRD_PARTY_MESSAGE,
                source_type="chat",
                adapter_id="fixture",
                adapter_version="1",
                occurred_at=clock.now - timedelta(hours=1),
                content="Рабочее сообщение, которое лаборатория не должна трогать.",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
                external_id="work-1",
            ),
        )

    async def scope_factory(label: str) -> Scope:
        scope = scope_of("test", f"space-test-{next(_ids)}")
        await store.provision(scope, lab_grants())
        return scope

    runner = LabRunner(
        storage=store,
        scope_factory=scope_factory,
        index_factory=lambda: SQLiteRecallIndex(base / f"{uuid4()}-index.sqlite"),
        watch=(work,),
        clock=clock,
    )
    return Env(store, work, runner, base)


def create_command(scope: Scope, markdown: str, request_id: str):
    payload = {
        "request_id": request_id,
        "scope": scope.model_dump(mode="json"),
        "target": scope.persona_id,
        "type": "persona.create",
        "arguments": {
            "schema_id": "persona.create-args",
            "schema_version": "1.0",
            "data": {"markdown": markdown},
        },
    }
    return from_wire(
        TypeAdapter(CreateCommand), json.dumps(payload).encode(), extensions=registry()
    )


def apply_command(scope: Scope, markdown: str, digest: str, version: int, request_id: str):
    payload = {
        "request_id": request_id,
        "scope": scope.model_dump(mode="json"),
        "target": scope.persona_id,
        "type": "persona.apply",
        "expected_version": version,
        "arguments": {
            "schema_id": "persona.apply-args",
            "schema_version": "1.0",
            "data": {"markdown": markdown, "revision_digest": digest},
        },
    }
    return from_wire(TypeAdapter(EditCommand), json.dumps(payload).encode(), extensions=registry())


async def persona_service(env: Env) -> PersonaService:
    return PersonaService(env.store, PERSONA, registry())


__all__ = ["REFERENCE_PROFILE"]
