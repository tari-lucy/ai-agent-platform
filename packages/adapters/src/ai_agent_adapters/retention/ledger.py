"""Independent deletion/expiry ledger: its own SQLite file; IDs, versions, reasons, times only."""

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_application.retention import LedgerEntry
from ai_agent_application.retention.model import Reason
from ai_agent_contracts.common import Scope

STRENGTH = {"expired": 0, "rights_revoked": 1, "operator_delete": 2}


def _key(scope: Scope) -> str:
    return "|".join(scope.partition)


class SQLiteDeletionLedger:
    """Keep this file apart from backups of the main database (restore gate, doc23)."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS deletion_ledger ("
            " scope_key TEXT NOT NULL, source_id TEXT NOT NULL, version INTEGER NOT NULL,"
            " reason TEXT NOT NULL"
            " CHECK (reason IN ('expired','rights_revoked','operator_delete')),"
            " blocked_at TEXT NOT NULL, purged_at TEXT, PRIMARY KEY (scope_key, source_id))"
        )

    def close(self) -> None:
        self._db.close()

    async def block(
        self, scope: Scope, source_id: str, version: int, reason: Reason, at: datetime
    ) -> LedgerEntry:
        def work() -> LedgerEntry:
            with self._lock:
                row = self._db.execute(
                    "SELECT version, reason, blocked_at, purged_at FROM deletion_ledger"
                    " WHERE scope_key=? AND source_id=?",
                    (_key(scope), source_id),
                ).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO deletion_ledger VALUES (?,?,?,?,?,NULL)",
                        (_key(scope), source_id, version, reason, at.isoformat()),
                    )
                    return LedgerEntry(source_id, version, reason, at, None)
                kept: Reason = reason if STRENGTH[reason] > STRENGTH[row[1]] else row[1]
                high = max(version, row[0])
                self._db.execute(
                    "UPDATE deletion_ledger SET reason=?, version=?"
                    " WHERE scope_key=? AND source_id=?",
                    (kept, high, _key(scope), source_id),
                )
                purged = datetime.fromisoformat(row[3]) if row[3] else None
                return LedgerEntry(source_id, high, kept, datetime.fromisoformat(row[2]), purged)

        return await asyncio.to_thread(work)

    async def mark_purged(self, scope: Scope, source_id: str, at: datetime) -> None:
        def work() -> None:
            with self._lock:
                self._db.execute(
                    "UPDATE deletion_ledger SET purged_at=? WHERE scope_key=? AND source_id=?"
                    " AND purged_at IS NULL",
                    (at.astimezone(UTC).isoformat(), _key(scope), source_id),
                )

        await asyncio.to_thread(work)

    async def entries(self, scope: Scope) -> list[LedgerEntry]:
        def work() -> list[LedgerEntry]:
            with self._lock:
                rows = self._db.execute(
                    "SELECT source_id, version, reason, blocked_at, purged_at FROM deletion_ledger"
                    " WHERE scope_key=? ORDER BY source_id",
                    (_key(scope),),
                ).fetchall()
            return [
                LedgerEntry(
                    r[0],
                    r[1],
                    r[2],
                    datetime.fromisoformat(r[3]),
                    datetime.fromisoformat(r[4]) if r[4] else None,
                )
                for r in rows
            ]

        return await asyncio.to_thread(work)
