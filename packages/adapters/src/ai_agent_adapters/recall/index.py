"""Derived FTS5 + sqlite-vec recall index in its OWN SQLite file (never the main database).

Everything here can be rebuilt from authoritative sources and is never trusted for access:
callers must recheck each candidate against the owner's repositories. Scope is part of every
key and every query is filtered by it before ranking. Vectors of different model signatures or
dimensions are never mixed. The index holds a derived copy of source text (FTS needs it), so
removal also uses ``secure_delete``; physical erasure from WAL/backups remains M5 scope.
"""

import asyncio
import importlib
import re
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_agent_application.memory.ports import (
    Candidate,
    ClaimLink,
    IndexedSource,
    SearchOutcome,
)
from ai_agent_contracts.common import Scope

from ai_agent_adapters.storage.policy import scope_key, stamp

STOPWORDS = frozenset(
    "а и в во на по с со у из к ко как какой какая какие где когда что чего "
    "чем почему ли не о об для от до после перед при это этого или можно "
    "его ее ещё есть было был была сколько".split()
)
MAX_QUERY_TOKENS = 24
BATCH = 500


def _sqlite_vec() -> Any:
    """The pinned, installed package only (optional extra); ImportError if absent."""
    return importlib.import_module("sqlite_vec")


def match_expression(query: str) -> str | None:
    """Free text -> safe FTS5 expression: quoted word tokens joined by OR, no operators."""
    tokens = list(dict.fromkeys(re.findall(r"\w+", query.lower())))
    kept = [token for token in tokens if token not in STOPWORDS][:MAX_QUERY_TOKENS]
    if not kept:
        return None
    return " OR ".join('"' + token.replace('"', "") + '"' for token in kept)


class SQLiteRecallIndex:
    def __init__(self, path: Path, *, dimension: int | None = None, busy_timeout_ms: int = 5000):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            path, isolation_level=None, timeout=busy_timeout_ms / 1000, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self.dimension = dimension
        self.vector_available = False
        if dimension is not None:
            self.vector_available = self._load_vector_extension()
        for statement in (
            f"PRAGMA busy_timeout={busy_timeout_ms}",
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=FULL",
            "PRAGMA secure_delete=ON",
        ):
            self._db.execute(statement)
        self._create_schema()

    # ---- setup ----------------------------------------------------------------------------

    def _load_vector_extension(self) -> bool:
        """Only the installed, pinned package is loaded; no path ever comes from user text."""
        try:
            sqlite_vec = _sqlite_vec()

            self._db.enable_load_extension(True)
            try:
                sqlite_vec.load(self._db)
            finally:
                self._db.enable_load_extension(False)
            return True
        except ImportError, AttributeError, sqlite3.Error:
            return False

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scope_meta(scope_key TEXT PRIMARY KEY, signature TEXT);
            CREATE TABLE IF NOT EXISTS sources(
                scope_key TEXT NOT NULL, source_id TEXT NOT NULL, version INTEGER NOT NULL,
                group_key TEXT NOT NULL, expires_at TEXT,
                PRIMARY KEY(scope_key, source_id));
            CREATE TABLE IF NOT EXISTS chunks(
                rowid INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL,
                source_id TEXT NOT NULL, chunk_no INTEGER NOT NULL, start INTEGER NOT NULL,
                "end" INTEGER NOT NULL, text_hash TEXT NOT NULL, has_vector INTEGER NOT NULL,
                UNIQUE(scope_key, source_id, chunk_no));
            CREATE TABLE IF NOT EXISTS claim_sources(
                scope_key TEXT NOT NULL, claim_id TEXT NOT NULL, source_id TEXT NOT NULL,
                PRIMARY KEY(scope_key, claim_id, source_id));
            CREATE TABLE IF NOT EXISTS claim_conflicts(
                scope_key TEXT NOT NULL, claim_id TEXT NOT NULL, other_id TEXT NOT NULL,
                PRIMARY KEY(scope_key, claim_id, other_id));
            CREATE VIRTUAL TABLE IF NOT EXISTS lexical
                USING fts5(text, tokenize='unicode61 remove_diacritics 2');
            """
        )
        if not self.vector_available or self.dimension is None:
            return
        row = self._db.execute("SELECT value FROM meta WHERE key='dimension'").fetchone()
        if row is not None and row["value"] != str(self.dimension):
            # Different dimension: discard vectors; text stays until the scope is reindexed.
            self._db.execute("DROP TABLE IF EXISTS vectors")
            self._db.execute("UPDATE scope_meta SET signature=NULL")
            self._db.execute("UPDATE chunks SET has_vector=0")
        self._db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vectors "
            f"USING vec0(embedding float[{int(self.dimension)}] distance_metric=cosine)"
        )
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES('dimension', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(self.dimension),),
        )

    # ---- plumbing -------------------------------------------------------------------------

    async def _run[T](self, function: Callable[..., T], *arguments: Any) -> T:
        def locked() -> T:
            with self._lock:
                return function(*arguments)

        return await asyncio.to_thread(locked)

    def _write[T](self, body: Callable[[], T]) -> T:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            result = body()
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return result

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- writes ---------------------------------------------------------------------------

    def _drop_chunks(self, key: str, source_id: str | None = None) -> None:
        query = "SELECT rowid FROM chunks WHERE scope_key=?"
        parameters: list[Any] = [key]
        if source_id is not None:
            query += " AND source_id=?"
            parameters.append(source_id)
        rowids = [row[0] for row in self._db.execute(query, parameters)]
        for start in range(0, len(rowids), BATCH):
            batch = rowids[start : start + BATCH]
            marks = ",".join("?" * len(batch))
            self._db.execute(f"DELETE FROM lexical WHERE rowid IN ({marks})", batch)
            if self.vector_available:
                self._db.execute(f"DELETE FROM vectors WHERE rowid IN ({marks})", batch)
            self._db.execute(f"DELETE FROM chunks WHERE rowid IN ({marks})", batch)

    def _vectors_allowed(self, key: str, signature: str | None, replacing: str | None) -> bool:
        if not self.vector_available or signature is None:
            return False
        row = self._db.execute(
            "SELECT signature FROM scope_meta WHERE scope_key=?", (key,)
        ).fetchone()
        if row is not None and row["signature"] is not None:
            return bool(row["signature"] == signature)
        others = self._db.execute(
            "SELECT COUNT(*) FROM sources WHERE scope_key=? AND source_id IS NOT ?",
            (key, replacing),
        ).fetchone()[0]
        if others:
            return False  # existing text-only entries: vectors would be partial under this label
        self._db.execute(
            "INSERT INTO scope_meta(scope_key, signature) VALUES(?, ?) "
            "ON CONFLICT(scope_key) DO UPDATE SET signature=excluded.signature",
            (key, signature),
        )
        return True

    def _insert(self, key: str, entry: IndexedSource, vectors: bool) -> None:
        sqlite_vec = _sqlite_vec()

        previous = self._db.execute(
            "SELECT group_key FROM sources WHERE scope_key=? AND source_id=?",
            (key, entry.source_id),
        ).fetchone()
        group = (
            previous["group_key"]
            if previous is not None
            else entry.group_key or "source:" + entry.source_id
        )
        self._drop_chunks(key, entry.source_id)
        self._db.execute(
            "INSERT OR REPLACE INTO sources(scope_key, source_id, version, group_key, expires_at) "
            "VALUES(?,?,?,?,?)",
            (
                key,
                entry.source_id,
                entry.version,
                group,
                stamp(entry.expires_at) if entry.expires_at else None,
            ),
        )
        for item in entry.chunks:
            embedded = vectors and item.embedding is not None
            cursor = self._db.execute(
                'INSERT INTO chunks(scope_key, source_id, chunk_no, start, "end", text_hash, '
                "has_vector) VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    entry.source_id,
                    item.chunk.no,
                    item.chunk.start,
                    item.chunk.end,
                    item.chunk.text_hash,
                    int(embedded),
                ),
            )
            self._db.execute(
                "INSERT INTO lexical(rowid, text) VALUES(?,?)", (cursor.lastrowid, item.chunk.text)
            )
            if embedded:
                assert item.embedding is not None
                if len(item.embedding) != self.dimension:
                    raise ValueError("EMBEDDING_DIMENSION")
                self._db.execute(
                    "INSERT INTO vectors(rowid, embedding) VALUES(?,?)",
                    (cursor.lastrowid, sqlite_vec.serialize_float32(list(item.embedding))),
                )

    async def put_source(self, scope: Scope, entry: IndexedSource, signature: str | None) -> None:
        key = scope_key(scope)

        def work() -> None:
            def body() -> None:
                allowed = self._vectors_allowed(key, signature, entry.source_id)
                self._insert(key, entry, allowed)

            self._write(body)

        await self._run(work)

    async def remove_source(self, scope: Scope, source_id: str) -> None:
        key = scope_key(scope)

        def work() -> None:
            def body() -> None:
                self._drop_chunks(key, source_id)
                self._db.execute(
                    "DELETE FROM sources WHERE scope_key=? AND source_id=?", (key, source_id)
                )

            self._write(body)

        await self._run(work)

    async def set_group(self, scope: Scope, source_ids: Sequence[str], group_key: str) -> None:
        key = scope_key(scope)

        def work() -> None:
            def body() -> None:
                for source_id in source_ids:
                    self._db.execute(
                        "UPDATE sources SET group_key=? WHERE scope_key=? AND source_id=? "
                        "AND (group_key LIKE 'source:%' OR ? < group_key)",
                        (group_key, key, source_id, group_key),
                    )

            self._write(body)

        await self._run(work)

    def _put_claim(self, key: str, link: ClaimLink) -> None:
        for table in ("claim_sources", "claim_conflicts"):
            self._db.execute(
                f"DELETE FROM {table} WHERE scope_key=? AND claim_id=?", (key, link.claim_id)
            )
        for source_id in link.source_ids:
            self._db.execute(
                "INSERT OR IGNORE INTO claim_sources VALUES(?,?,?)", (key, link.claim_id, source_id)
            )
        for other in link.conflict_ids:
            self._db.execute(
                "INSERT OR IGNORE INTO claim_conflicts VALUES(?,?,?)", (key, link.claim_id, other)
            )

    async def put_claim(self, scope: Scope, link: ClaimLink) -> None:
        key = scope_key(scope)
        await self._run(lambda: self._write(lambda: self._put_claim(key, link)))

    async def replace_scope(
        self,
        scope: Scope,
        sources: Sequence[IndexedSource],
        claims: Sequence[ClaimLink],
        signature: str | None,
    ) -> None:
        key = scope_key(scope)

        def body() -> None:
            self._drop_chunks(key)
            for table in ("sources", "claim_sources", "claim_conflicts", "scope_meta"):
                self._db.execute(f"DELETE FROM {table} WHERE scope_key=?", (key,))
            complete = (
                self.vector_available
                and signature is not None
                and all(item.embedding is not None for entry in sources for item in entry.chunks)
            )
            if complete:
                self._db.execute(
                    "INSERT INTO scope_meta(scope_key, signature) VALUES(?,?)", (key, signature)
                )
            for entry in sources:
                self._insert(key, entry, complete)
            for link in claims:
                self._put_claim(key, link)

        await self._run(lambda: self._write(body))

    async def purge_expired(self, now: datetime) -> int:
        limit = stamp(now)

        def body() -> int:
            rows = self._db.execute(
                "SELECT scope_key, source_id FROM sources WHERE expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (limit,),
            ).fetchall()
            for row in rows:
                self._drop_chunks(row["scope_key"], row["source_id"])
                self._db.execute(
                    "DELETE FROM sources WHERE scope_key=? AND source_id=?",
                    (row["scope_key"], row["source_id"]),
                )
            return len(rows)

        return await self._run(lambda: self._write(body))

    async def compact(self) -> None:
        def work() -> None:
            self._db.execute("INSERT INTO lexical(lexical) VALUES('optimize')")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        await self._run(work)

    # ---- reads ----------------------------------------------------------------------------

    async def inventory(self, scope: Scope) -> dict[str, int]:
        key = scope_key(scope)

        def work() -> dict[str, int]:
            rows = self._db.execute(
                "SELECT source_id, version FROM sources WHERE scope_key=?", (key,)
            )
            return {row["source_id"]: int(row["version"]) for row in rows}

        return await self._run(work)

    async def conflicts_for(
        self, scope: Scope, source_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        key = scope_key(scope)

        def work() -> dict[str, tuple[str, ...]]:
            found: dict[str, list[str]] = {}
            for start in range(0, len(source_ids), BATCH):
                batch = list(source_ids[start : start + BATCH])
                marks = ",".join("?" * len(batch))
                rows = self._db.execute(
                    f"SELECT DISTINCT cs.source_id, cs.claim_id FROM claim_sources cs "
                    f"WHERE cs.scope_key=? AND cs.source_id IN ({marks}) AND EXISTS("
                    f"SELECT 1 FROM claim_conflicts cc WHERE cc.scope_key=cs.scope_key "
                    f"AND cc.claim_id=cs.claim_id) ORDER BY cs.claim_id",
                    [key, *batch],
                )
                for row in rows:
                    found.setdefault(row["source_id"], []).append(row["claim_id"])
            return {source_id: tuple(ids) for source_id, ids in found.items()}

        return await self._run(work)

    async def search(
        self,
        scope: Scope,
        query: str,
        embedding: Sequence[float] | None,
        signature: str | None,
        *,
        limit: int,
        now: datetime,
    ) -> SearchOutcome:
        key, moment = scope_key(scope), stamp(now)

        def candidate(row: sqlite3.Row) -> Candidate:
            return Candidate(
                row["source_id"],
                int(row["version"]),
                int(row["chunk_no"]),
                int(row["start"]),
                int(row["end"]),
                row["text_hash"],
                row["group_key"],
            )

        columns = (
            'c.rowid AS rid, c.source_id, s.version, c.chunk_no, c.start, c."end" AS "end", '
            "c.text_hash, s.group_key"
        )
        live = "c.scope_key=? AND (s.expires_at IS NULL OR s.expires_at > ?)"
        join = "JOIN sources s ON s.scope_key=c.scope_key AND s.source_id=c.source_id"

        def work() -> SearchOutcome:
            lexical: list[Candidate] = []
            expression = match_expression(query)
            if expression is not None:
                rows = self._db.execute(
                    f"SELECT {columns} FROM lexical JOIN chunks c ON c.rowid=lexical.rowid {join} "
                    f"WHERE lexical MATCH ? AND {live} ORDER BY bm25(lexical), c.rowid LIMIT ?",
                    (expression, key, moment, limit),
                )
                lexical = [candidate(row) for row in rows]
            vector: list[Candidate] = []
            used = complete = False
            scope_row = self._db.execute(
                "SELECT signature FROM scope_meta WHERE scope_key=?", (key,)
            ).fetchone()
            matches = (
                self.vector_available
                and embedding is not None
                and signature is not None
                and scope_row is not None
                and scope_row["signature"] == signature
            )
            if matches:
                assert embedding is not None
                if len(embedding) != self.dimension:
                    raise ValueError("EMBEDDING_DIMENSION")
                sqlite_vec = _sqlite_vec()

                used = True
                complete = not self._db.execute(
                    "SELECT 1 FROM chunks WHERE scope_key=? AND has_vector=0 LIMIT 1", (key,)
                ).fetchone()
                ranked = [
                    row[0]
                    for row in self._db.execute(
                        "SELECT rowid FROM vectors WHERE embedding MATCH ? AND k=? AND rowid IN("
                        f"SELECT c.rowid FROM chunks c {join} WHERE {live}) ORDER BY distance",
                        (sqlite_vec.serialize_float32(list(embedding)), limit, key, moment),
                    )
                ]
                for rowid in ranked:
                    row = self._db.execute(
                        f"SELECT {columns} FROM chunks c {join} WHERE c.rowid=?", (rowid,)
                    ).fetchone()
                    if row is not None:
                        vector.append(candidate(row))
            return SearchOutcome(tuple(lexical), tuple(vector), used, complete if used else True)

        return await self._run(work)
