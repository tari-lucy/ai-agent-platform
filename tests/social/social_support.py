"""Fixtures for M2-03: real SQLite authority, invented people and text only."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.social import (
    SocialPolicy,
    SocialService,
    register_social_extensions,
)
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import from_wire
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Delivery
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import Provenance
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)

MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
SOCIAL = Principal("owner-social", "core_owner", Owner.SOCIAL)
CHANNEL = Principal("owner-channel", "core_owner", Owner.CHANNEL)
OPERATOR = Principal("operator-synthetic", "operator")
WORKER = Principal("worker-synthetic", "worker")
GRANTS = (
    Grant(MEMORY, Owner.MEMORY, read=True, write=True),
    Grant(SOCIAL, Owner.SOCIAL, read=True, write=True),
    Grant(SOCIAL, Owner.MEMORY, read=True),
    Grant(SOCIAL, Owner.CHANNEL, read=True),
    Grant(CHANNEL, Owner.CHANNEL, read=True, write=True),
    Grant(OPERATOR, Owner.SOCIAL, read=True),
)
_ids = count(1)


def run(coroutine):
    return asyncio.run(coroutine)


def rid() -> str:
    return f"req-{next(_ids)}"


def make_scope(persona="persona-a", mode="work", space=None, workspace="synthetic-workspace"):
    return Scope(
        workspace_id=workspace,
        persona_id=persona,
        memory_space_id=space or f"space-{persona}-{mode}",
        memory_mode=mode,
    )


def command(scope, request_id=None):
    return MessageCommand(
        request_id=request_id or rid(),
        scope=scope,
        target="synthetic-target",
        type="dialogue.message",
        arguments=MessageArguments(text="Вымышленная команда."),
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class World:
    def __init__(self, store, memory, social, clock, registry):
        self.store, self.memory, self.social, self.clock = store, memory, social, clock
        self.registry = registry

    def restarted(self) -> SocialService:
        """A fresh service over the same database: what a process restart looks like."""
        return SocialService(
            self.store,
            SOCIAL,
            self.registry,
            clock=self.clock,
            policy=self.social.policy,
            reader_access=self.social.reader_access,
        )

    async def provision(self, scope):
        await self.store.provision(scope, GRANTS)

    async def source(self, scope, text, external_id=None) -> SourceRef:
        made = await self.memory.register_source(
            command(scope),
            SourceDraft(
                provenance=Provenance.THIRD_PARTY_MESSAGE
                if scope.memory_mode == "work"
                else Provenance.SIMULATION,
                source_type="synthetic-chat",
                adapter_id="fixture",
                adapter_version="1",
                occurred_at=self.clock.now - timedelta(hours=1),
                content=text,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
                external_id=external_id or rid(),
                author_id="person-x",
            ),
        )
        return SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )

    async def delivery(self, scope, delivery_id, utterance_id, status="DELIVERED"):
        stamp = self.clock.now.isoformat()
        record = from_wire(
            TypeAdapter(Delivery),
            __import__("json")
            .dumps(
                {
                    "id": delivery_id,
                    "scope": scope.model_dump(mode="json"),
                    "created_at": stamp,
                    "updated_at": stamp,
                    "version": 1,
                    "utterance_id": utterance_id,
                    "revision": 1,
                    "channel_id": "channel-" + delivery_id,
                    "attempt_id": "attempt-" + delivery_id,
                    "status": status,
                }
            )
            .encode(),
        )
        cmd = command(scope)
        async with self.store.transaction(CHANNEL, scope, cmd) as unit:
            await unit.put(record, access=(RecordAccess(SOCIAL.id),))
            await unit.complete(
                Receipt(
                    request_id=cmd.request_id,
                    scope=scope,
                    status="completed",
                    resource_id=delivery_id,
                    version=1,
                )
            )

    async def person(self, scope, account="acct-1", platform="twitch", name="Ника"):
        evidence = await self.source(scope, f"Привет, я {name}.")
        found = await self.social.observe_account(command(scope), platform, account, name, evidence)
        assert found is not None
        return found.person, evidence


async def new_world(tmp_path, scopes, *, policy=None, reader_access=True) -> World:
    base = Path(os.environ.get("M2_03_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    registry = ExtensionRegistry()
    register_social_extensions(registry)
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=clock, extensions=registry)
    await store.upgrade(MIGRATIONS)
    memory = MemoryService(
        store,
        MEMORY,
        registry,
        clock=clock,
        reader_access=(RecordAccess(SOCIAL.id),) if reader_access else (),
    )
    social = SocialService(store, SOCIAL, registry, clock=clock, policy=policy)
    world = World(store, memory, social, clock, registry)
    for scope in scopes:
        await world.provision(scope)
    return world


__all__ = ["SocialPolicy"]
