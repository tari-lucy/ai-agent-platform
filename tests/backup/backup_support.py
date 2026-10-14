"""Fixtures for M5-02: real SQLite files, real index/ledger files, virtual time, invented text."""

import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "retention"))

from ai_agent_adapters.backup import (  # noqa: E402
    DirectoryBackupStore,
    FileSwitcher,
    SQLiteRestoreTarget,
    SQLiteSnapshotter,
    SQLiteStagingFactory,
)
from ai_agent_adapters.recall import SQLiteRecallIndex  # noqa: E402
from ai_agent_adapters.retention import SQLitePhysicalStore  # noqa: E402
from ai_agent_adapters.storage.database import SQLiteStore  # noqa: E402
from ai_agent_application.backup import BackupPolicy, BackupService  # noqa: E402
from ai_agent_application.memory import IndexHealth, MemoryService, RecallService  # noqa: E402
from ai_agent_application.retention import RetentionService  # noqa: E402
from retention_support import (  # noqa: E402
    MEMORY,
    NOW,
    SOCIAL,
    command,
    make_scope,
    new_world,
    residue,
    run,
)

HEAD = "0003"

__all__ = [
    "HEAD",
    "MEMORY",
    "NOW",
    "SOCIAL",
    "BWorld",
    "Gate",
    "command",
    "make_scope",
    "new_bworld",
    "residue",
    "run",
    "reopen",
]


class Gate:
    def __init__(self) -> None:
        self.state = "STOPPED"

    async def run_state(self) -> str:
        return self.state


class Extra:
    def __init__(self) -> None:
        self.items: list[tuple[str, Path]] = []

    def files(self):
        return list(self.items)


class BWorld:
    pass


async def new_bworld(tmp_path, scopes, *, policy=None, quota=None, upgrade_staged=None):
    world = await new_world(tmp_path, scopes)
    base = tmp_path / f"b-{uuid4().hex[:6]}"
    base.mkdir()
    live_db, live_index = world.paths
    gate, extra = Gate(), Extra()
    snapshotter = SQLiteSnapshotter(live_db)
    store = DirectoryBackupStore(base / "copies")

    async def build(database: Path) -> SQLiteRestoreTarget:
        staged = SQLiteStore(database, clock=world.clock, extensions=world.store.extensions)
        index = SQLiteRecallIndex(
            database.parent / "index.sqlite", dimension=world.embedder.dimension
        )
        memory = MemoryService(
            staged,
            MEMORY,
            world.store.extensions,
            index=index,
            embedder=world.embedder,
            health=IndexHealth(),
            clock=world.clock,
        )
        physical = SQLitePhysicalStore(staged)
        retention = RetentionService(
            staged,
            MEMORY,
            memory,
            physical,
            world.ledger,
            index=index,
            policy=world.policy,
            clock=world.clock,
        )
        return SQLiteRestoreTarget(staged, retention, memory, physical, index.close)

    async def close_live():
        world.index.close()
        await world.store.close()

    switcher = FileSwitcher(live_db, live_index, base / "replaced")
    service = BackupService(
        snapshotter,
        store,
        world.ledger,
        gate,
        SQLiteStagingFactory(build),
        switcher,
        work_dir=base / "work",
        current_head=HEAD,
        close_live=close_live,
        ledger_dir=base / "ledger-copies",
        extra=extra,
        upgrade_staged=upgrade_staged,
        policy=policy or BackupPolicy(quota_bytes=quota),
        clock=world.clock,
        software="test",
    )
    (base / "work").mkdir()
    bw = BWorld()
    bw.world, bw.service, bw.gate, bw.extra = world, service, gate, extra
    bw.store, bw.switcher, bw.base, bw.snapshotter = store, switcher, base, snapshotter
    return bw


async def reopen(bw):
    """The live database as the next process would open it after a restore."""
    world = bw.world
    live_db, live_index = world.paths
    store = SQLiteStore(live_db, clock=world.clock, extensions=world.store.extensions)
    index = SQLiteRecallIndex(live_index, dimension=world.embedder.dimension)
    health = IndexHealth()
    memory = MemoryService(
        store,
        MEMORY,
        store.extensions,
        index=index,
        embedder=world.embedder,
        health=health,
        clock=world.clock,
    )
    recall = RecallService(
        store, MEMORY, index, embedder=world.embedder, health=health, clock=world.clock
    )
    return store, memory, recall, SQLitePhysicalStore(store), index
