"""One recall-index file: recognise it, read what it holds, remove sources through its own API."""

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_contracts.common import Scope

from .roots import ArtifactsError

TABLES = frozenset({"meta", "scope_meta", "sources", "chunks", "lexical"})


def _readonly(path: Path) -> sqlite3.Connection:
    # Without a -wal file there is nothing to recover, and the read leaves no file behind.
    mode = "mode=ro" if Path(str(path) + "-wal").exists() else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.as_uri()}?{mode}", uri=True)


def recognised(path: Path) -> bool:
    """Only a real recall index is opened for writing: a foreign file must not gain tables."""
    try:
        connection = _readonly(path)
        try:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return TABLES <= tables and "vectors" not in tables  # vector rows need the owner's handle


def held(path: Path) -> set[tuple[str, str]]:
    """(scope key, source id) of every source in the file, read without changing it."""
    try:
        connection = _readonly(path)
        try:
            rows = connection.execute("SELECT scope_key, source_id FROM sources").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        raise ArtifactsError("INDEX_UNREADABLE") from None
    return {(str(a), str(b)) for a, b in rows}


async def purge_closed(path: Path, scope: Scope, source_ids: Iterable[str]) -> None:
    """Open a closed file briefly through the adapter, remove, compact, close. Never unlinks."""
    wanted = tuple(source_ids)
    try:
        index = SQLiteRecallIndex(path)
    except sqlite3.Error:
        raise ArtifactsError("INDEX_BUSY") from None
    try:
        present = await index.inventory(scope)
        for source_id in wanted:
            if source_id in present:
                await index.remove_source(scope, source_id)
        await index.compact()
        if set(wanted) & set(await index.inventory(scope)):
            raise ArtifactsError("PURGE_INCOMPLETE")
    except sqlite3.Error:
        raise ArtifactsError("INDEX_BUSY") from None
    finally:
        index.close()
