"""A restored database opened in staging: ledger gate, reindex, outbox quarantine, checks."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from ai_agent_application.memory import MemoryService
from ai_agent_application.retention import LedgerApplication, RetentionService, SweepReport
from ai_agent_contracts.common import Scope
from sqlalchemy import text

from ai_agent_adapters.retention import SQLitePhysicalStore
from ai_agent_adapters.storage.database import SQLiteStore

OPEN_STATES = ("PROPOSED", "AWAITING_APPROVAL", "READY", "QUEUED", "RUNNING", "WAITING")
LIVE_OUTBOX = ("pending", "delivered")


class SQLiteRestoreTarget:
    def __init__(
        self,
        store: SQLiteStore,
        retention: RetentionService,
        memory: MemoryService,
        physical: SQLitePhysicalStore,
        close_index: Callable[[], None] | None = None,
    ) -> None:
        self.store, self.retention, self.memory = store, retention, memory
        self.physical, self.close_index = physical, close_index

    async def scopes(self) -> list[Scope]:
        async with self.store.connection() as connection:
            rows = await connection.execute(
                text(
                    "SELECT workspace_id, persona_id, memory_space_id, memory_mode"
                    " FROM scopes ORDER BY scope_id"
                )
            )
            return [
                Scope(workspace_id=r[0], persona_id=r[1], memory_space_id=r[2], memory_mode=r[3])
                for r in rows.all()
            ]

    async def apply_ledger(self, scope: Scope) -> LedgerApplication:
        return await self.retention.apply_ledger(scope)

    async def sweep(self, scope: Scope) -> SweepReport:
        return await self.retention.sweep(scope)

    async def rebuild_index(self, scope: Scope) -> int:
        return await self.memory.reindex_scope(scope) if self.memory.index is not None else 0

    async def quarantine_outbox(self) -> int:
        """Restored events must not replay: old external commands are never executed again."""
        async with self.store.connection(write=True) as connection:
            result = await connection.execute(
                text(
                    "UPDATE outbox SET status='failed', next_attempt_at=NULL"
                    " WHERE status IN ('pending','delivered')"
                )
            )
        return int(result.rowcount)

    async def operations_at_risk(self) -> int:
        async with self.store.connection() as connection:
            states = ",".join(f"'{s}'" for s in OPEN_STATES)
            row = await connection.execute(
                text(
                    "SELECT COUNT(*) FROM records r JOIN record_revisions v"
                    " ON r.scope_id=v.scope_id AND r.kind=v.kind AND r.id=v.id"
                    " AND r.version=v.version WHERE r.kind='Job'"
                    f" AND json_extract(v.payload,'$.status') IN ({states})"
                )
            )
            return int(row.scalar_one())

    async def pending_outbox(self) -> int:
        async with self.store.connection() as connection:
            row = await connection.execute(
                text("SELECT COUNT(*) FROM outbox WHERE status IN ('pending','delivered')")
            )
            return int(row.scalar_one())

    async def blocked_text_remaining(self, scope: Scope) -> list[str]:
        found = []
        for entry in await self.retention.ledger.entries(scope):
            stub = await self.physical.source(scope, entry.source_id)
            if stub is not None and (stub.has_content or stub.availability == "available"):
                found.append(entry.source_id)
        return found

    async def close(self) -> None:
        await self.store.close()
        if self.close_index is not None:
            self.close_index()

        def single_file() -> None:  # a self-contained database file for the switch
            connection = sqlite3.connect(str(self.store.engine.url.database))
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
            finally:
                connection.close()

        await asyncio.to_thread(single_file)


class SQLiteStagingFactory:
    """``build`` is supplied by the composition root: principals, extensions, embedder, index."""

    def __init__(self, build: Callable[[Path], Awaitable[SQLiteRestoreTarget]]) -> None:
        self.build = build

    async def open(self, database: Path) -> SQLiteRestoreTarget:
        return await self.build(database)
