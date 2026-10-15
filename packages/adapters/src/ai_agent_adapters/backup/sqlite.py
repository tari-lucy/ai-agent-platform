"""Consistent snapshot of a live SQLite database through the engine's backup API.

A file copy of a WAL database loses committed pages that still sit in ``-wal``; the backup API
reads one consistent snapshot while writers continue. The copy is made self-contained
(``journal_mode=DELETE``) and checked before anything keeps it.
"""

import asyncio
import sqlite3
from pathlib import Path

from ai_agent_adapters.local_files import restrict_private


class SQLiteSnapshotter:
    def __init__(self, live: Path) -> None:
        self.live = live

    async def snapshot(self, target: Path) -> None:
        def work() -> None:
            restrict_private(target.parent, directory=True)
            source = sqlite3.connect(self.live, timeout=30)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)  # one consistent read snapshot
                destination.execute("PRAGMA journal_mode=DELETE")
            finally:
                destination.close()
                source.close()
            restrict_private(target, directory=False)

        await asyncio.to_thread(work)

    async def head(self, database: Path) -> str:
        def work() -> str:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            finally:
                connection.close()
            return str(row[0]) if row else ""

        return await asyncio.to_thread(work)

    async def integrity(self, database: Path) -> list[str]:
        def work() -> list[str]:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                found = [str(r[0]) for r in connection.execute("PRAGMA integrity_check")]
                broken = connection.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                connection.close()
            problems = [] if found == ["ok"] else found
            return problems + (["FOREIGN_KEY_VIOLATIONS"] if broken else [])

        try:
            return await asyncio.to_thread(work)
        except sqlite3.DatabaseError:
            return ["UNREADABLE"]
