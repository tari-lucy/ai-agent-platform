"""Fixtures for M5-01: real SQLite authority, real index and ledger files, invented text only."""

import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "memory"))
sys.path.insert(0, str(ROOT / "tests" / "usage"))

from ai_agent_adapters.recall import SQLiteRecallIndex  # noqa: E402
from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore  # noqa: E402
from ai_agent_adapters.storage.database import SQLiteStore  # noqa: E402
from ai_agent_application.memory import IndexHealth, MemoryService, RecallService  # noqa: E402
from ai_agent_application.retention import RetentionPolicy, RetentionService  # noqa: E402
from ai_agent_application.storage import Grant, Owner, Principal  # noqa: E402
from ai_agent_contracts.api import Receipt  # noqa: E402
from ai_agent_contracts.common import Contract  # noqa: E402
from ai_agent_contracts.control import OperationProjection  # noqa: E402
from ai_agent_contracts.extensions import ExtensionRegistry  # noqa: E402
from ai_agent_contracts.states import OperationState  # noqa: E402
from memory_support import (  # noqa: E402
    GRANTS as MEMORY_GRANTS,
)
from memory_support import (  # noqa: E402
    MEMORY,
    NOW,
    Clock,
    FakeEmbedder,
    World,
    command,
    make_scope,
    rid,
    run,
)

MIGRATIONS = ROOT / "migrations"
LLM = Principal("owner-llm", "core_owner", Owner.LLM)
JOBS = Principal("owner-jobs", "core_owner", Owner.JOBS)
GRANTS = (
    *MEMORY_GRANTS,
    Grant(LLM, Owner.LLM, read=True, write=True),
    Grant(JOBS, Owner.JOBS, read=True, write=True),
)

__all__ = [
    "GRANTS",
    "JOBS",
    "LLM",
    "MEMORY",
    "NOW",
    "RetWorld",
    "command",
    "make_scope",
    "new_world",
    "residue",
    "rid",
    "run",
    "timedelta",
]


class RetWorld(World):
    """Memory world plus the retention owner, its physical store and the independent ledger."""

    def __init__(self, *args, retention, physical, ledger, paths, policy):
        super().__init__(*args)
        self.retention, self.physical, self.ledger = retention, physical, ledger
        self.paths, self.policy = paths, policy

    def restarted_service(self, **overrides):
        kwargs = dict(index=self.index, policy=self.policy, clock=self.clock)
        kwargs.update(overrides)
        return RetentionService(
            self.store, MEMORY, self.memory, self.physical, self.ledger, **kwargs
        )

    async def put_projection(self, scope, name, status, *, age_days, request_id=None):
        at = self.clock.now - timedelta(days=age_days)
        record = OperationProjection.model_validate(
            {
                "id": name,
                "scope": scope.model_dump(),
                "created_at": at,
                "updated_at": at,
                "version": 1,
                "operation_id": name,
                "status": OperationState(status),
                "deadline": at + timedelta(hours=1),
                "result_id": "res-1" if status == "SUCCEEDED" else None,
            }
        )
        cmd = command(scope, request_id)
        async with self.store.transaction(JOBS, scope, cmd) as unit:
            await unit.put(record)
            await unit.complete(
                Receipt(
                    request_id=cmd.request_id,
                    scope=scope,
                    status="completed",
                    resource_id=name,
                    version=1,
                )
            )


def residue(paths, needle: str) -> list[str]:
    """Every file where the removed text is still physically present (db, WAL, SHM, index)."""
    found = []
    raw = needle.encode()
    for base in paths:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(base) + suffix)
            if path.exists() and raw in path.read_bytes():
                found.append(path.name)
    return found


async def new_world(tmp_path, scopes, *, policy=None) -> RetWorld:
    base = Path(os.environ.get("M5_01_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    registry = ExtensionRegistry()

    class Fixture(Contract):
        max_output_tokens: int

    registry.register("usage.fixture-parameters", Fixture)
    db_path = base / f"{uuid4()}.sqlite"
    index_path = base / f"{uuid4()}-index.sqlite"
    ledger_path = base / f"{uuid4()}-ledger.sqlite"
    store = SQLiteStore(db_path, clock=clock, extensions=registry)
    await store.upgrade(MIGRATIONS)
    embedder = FakeEmbedder()
    index = SQLiteRecallIndex(index_path, dimension=embedder.dimension)
    health = IndexHealth()
    memory = MemoryService(
        store,
        MEMORY,
        registry,
        index=index,
        embedder=embedder,
        health=health,
        clock=clock,
    )
    recall = RecallService(store, MEMORY, index, embedder=embedder, health=health, clock=clock)
    physical = SQLitePhysicalStore(store)
    await physical.install()
    ledger = SQLiteDeletionLedger(ledger_path)
    policy = policy or RetentionPolicy()
    retention = RetentionService(
        store, MEMORY, memory, physical, ledger, index=index, policy=policy, clock=clock
    )
    world = RetWorld(
        store,
        index,
        memory,
        recall,
        health,
        embedder,
        clock,
        retention=retention,
        physical=physical,
        ledger=ledger,
        paths=(db_path, index_path),
        policy=policy,
    )
    for scope in scopes:
        await store.provision(scope, GRANTS)
    return world
