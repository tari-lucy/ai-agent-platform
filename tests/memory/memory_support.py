"""Shared fixtures for M2-02 tests: real SQLite stores, real FTS5/sqlite-vec, invented text only."""

import asyncio
import os
import re
import zlib
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.memory import (
    IndexHealth,
    MemoryService,
    RecallService,
    SourceDraft,
)
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import MessageArguments, MessageCommand
from ai_agent_contracts.common import Scope
from ai_agent_contracts.memory import RecallRequest
from ai_agent_contracts.states import Provenance

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)

MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
OPERATOR = Principal("operator-synthetic", "operator")
WORKER = Principal("worker-synthetic", "worker")
GRANTS = (
    Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True, public_disclosure=True),
    Grant(OPERATOR, Owner.MEMORY, read=True),
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


class FakeEmbedder:
    """Deterministic bag of 4-letter word stems: inflected forms share a vector direction."""

    dimension = 32

    def __init__(self, signature="fake-stem/1/32", dimension=32):
        self.signature = signature
        self.dimension = dimension
        self.fail = False
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedder unavailable")
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimension
            for word in re.findall(r"\w+", text.lower()):
                vector[zlib.crc32(word[:4].encode()) % self.dimension] += 1.0
            norm = sum(x * x for x in vector) ** 0.5 or 1.0
            vectors.append([x / norm for x in vector])
        return vectors


class FailingIndex:
    """Index whose writes fail: the authority must stay committed and the lag be visible."""

    def __init__(self, inner):
        self.inner = inner
        self.broken = True

    def __getattr__(self, name):
        target = getattr(self.inner, name)

        async def guarded(*args, **kwargs):
            if self.broken and name in {"put_source", "remove_source", "set_group", "put_claim"}:
                raise RuntimeError("index write failed")
            return await target(*args, **kwargs)

        return guarded


class World:
    def __init__(self, store, index, memory, recall, health, embedder, clock):
        self.store, self.index, self.memory, self.recall_service = store, index, memory, recall
        self.health, self.embedder, self.clock = health, embedder, clock

    async def provision(self, scope, grants=GRANTS):
        await self.store.provision(scope, grants)

    async def add(self, scope, text, *, external_id=None, service=None, **kwargs):
        defaults = dict(
            provenance=Provenance.THIRD_PARTY_MESSAGE
            if scope.memory_mode == "work"
            else Provenance.SIMULATION,
            source_type="synthetic-chat",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=NOW - timedelta(days=1),
            content=text,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
            external_id=external_id,
            author_id="person-1",
        )
        defaults.update(kwargs)
        target = service or self.memory
        return await target.register_source(command(scope), SourceDraft(**defaults))

    async def ask(self, scope, query, *, purpose="private_context", service=None, **kwargs):
        request = RecallRequest(scope=scope, query=query, purpose=purpose, **kwargs)
        return await (service or self.recall_service).recall(request)


async def new_world(
    tmp_path, scopes, *, embedder="default", reader_access=(), with_index=True
) -> World:
    base = Path(os.environ.get("M2_02_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=clock)
    await store.upgrade(MIGRATIONS)
    fake = FakeEmbedder() if embedder == "default" else embedder
    index = (
        SQLiteRecallIndex(
            base / f"{uuid4()}-index.sqlite", dimension=fake.dimension if fake else None
        )
        if with_index
        else None
    )
    health = IndexHealth()
    memory = MemoryService(
        store,
        MEMORY,
        store.extensions or _registry(),
        index=index,
        embedder=fake,
        health=health,
        clock=clock,
        reader_access=reader_access,
    )
    recall = RecallService(store, MEMORY, index, embedder=fake, health=health, clock=clock)
    world = World(store, index, memory, recall, health, fake, clock)
    for scope in scopes:
        await world.provision(scope)
    return world


def _registry():
    from ai_agent_contracts.extensions import ExtensionRegistry

    return ExtensionRegistry()


__all__ = ["RecordAccess"]
