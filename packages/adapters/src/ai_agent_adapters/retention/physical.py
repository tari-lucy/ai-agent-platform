"""Physical erasure inside the authoritative SQLite store (M5-01).

Works beside the owner API because storage hides TTL-expired rows even from their owner. It only
removes text, flips visibility flags and deletes whole expired metadata records; it never creates
or revises business records. ``finalize`` then overwrites freed pages, empties the WAL and compacts
the file, so removed text is not left in free pages (``secure_delete`` + checkpoint + VACUUM).
"""

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from ai_agent_application.retention import SourceStub
from ai_agent_contracts.common import Scope
from sqlalchemy import and_, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key, stamp

ERASABLE = ("Claim", "Summary", "Episode")  # memory-owned derivatives of source text
TERMINAL = ("SUCCEEDED", "REJECTED", "FAILED", "CANCELLED", "EXPIRED")  # OUTCOME_UNKNOWN is kept
AGGREGATES = """
CREATE TABLE IF NOT EXISTS retention_usage_aggregates (
  scope_id TEXT NOT NULL, month TEXT NOT NULL, role_id TEXT NOT NULL, status TEXT NOT NULL,
  currency TEXT NOT NULL, completeness TEXT NOT NULL,
  attempts INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER NOT NULL,
  unknown_usage INTEGER NOT NULL, amount TEXT NOT NULL, unknown_amount INTEGER NOT NULL,
  PRIMARY KEY (scope_id, month, role_id, status, currency, completeness))
"""


def _month(value: str) -> str:
    return value[:7]


class SQLitePhysicalStore:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.path = str(store.engine.url.database)

    async def install(self) -> None:
        """Explicit administration step (like ``upgrade``); never run implicitly."""
        async with self.store.connection(write=True) as connection:
            await connection.execute(text(AGGREGATES))

    async def _write(self) -> Any:
        return self.store.connection(write=True)

    async def _secure(self, connection: AsyncConnection) -> None:
        await connection.exec_driver_sql("PRAGMA secure_delete=ON")

    # ---- reads ----------------------------------------------------------------------------

    def _stub(self, row: Any) -> SourceStub:
        payload = json.loads(row["payload"])
        retention = payload.get("retention", {})
        expires = row["expires_at"]
        return SourceStub(
            source_id=row["id"],
            version=row["version"],
            availability=payload.get("availability", "available"),
            expires_at=datetime.fromisoformat(expires) if expires else None,
            updated_at=datetime.fromisoformat(row["updated_at"]),
            protected=bool(retention.get("protected")),
            has_content=payload.get("content") is not None or payload.get("artifact") is not None,
        )

    def _current(self, key: str) -> Any:
        return (
            select(
                db.records.c.id,
                db.records.c.version,
                db.records.c.expires_at,
                db.records.c.updated_at,
                db.revisions.c.payload,
            )
            .select_from(
                db.records.join(
                    db.revisions,
                    and_(
                        db.records.c.scope_id == db.revisions.c.scope_id,
                        db.records.c.kind == db.revisions.c.kind,
                        db.records.c.id == db.revisions.c.id,
                        db.records.c.version == db.revisions.c.version,
                    ),
                )
            )
            .where(db.records.c.scope_id == key, db.records.c.kind == "Source")
        )

    async def source(self, scope: Scope, source_id: str) -> SourceStub | None:
        await self.store.check_schema()
        async with self.store.connection() as connection:
            row = (
                (
                    await connection.execute(
                        self._current(scope_key(scope)).where(db.records.c.id == source_id)
                    )
                )
                .mappings()
                .first()
            )
        return self._stub(row) if row else None

    async def due_sources(
        self, scope: Scope, *, now: datetime, test_cutoff: datetime | None
    ) -> list[SourceStub]:
        await self.store.check_schema()
        query = self._current(scope_key(scope)).where(db.records.c.available.is_(True))
        due = and_(db.records.c.expires_at.is_not(None), db.records.c.expires_at <= stamp(now))
        if test_cutoff is not None:
            due = due | (db.records.c.updated_at <= stamp(test_cutoff))
        async with self.store.connection() as connection:
            rows = (
                (await connection.execute(query.where(due).order_by(db.records.c.id)))
                .mappings()
                .all()
            )
        return [self._stub(row) for row in rows]

    async def _links(
        self, connection: AsyncConnection, key: str, source_ids: Sequence[str]
    ) -> dict[tuple[str, str], set[str]]:
        """Current-revision basis of every record that cites one of the sources."""
        cited = (
            select(db.links.c.kind, db.links.c.id)
            .join(
                db.records,
                and_(
                    db.records.c.scope_id == db.links.c.scope_id,
                    db.records.c.kind == db.links.c.kind,
                    db.records.c.id == db.links.c.id,
                    db.records.c.version == db.links.c.version,
                ),
            )
            .where(db.links.c.scope_id == key, db.links.c.source_id.in_(list(source_ids)))
            .distinct()
        )
        basis: dict[tuple[str, str], set[str]] = defaultdict(set)
        for kind, record_id in (await connection.execute(cited)).all():
            rows = await connection.execute(
                select(db.links.c.source_id)
                .join(
                    db.records,
                    and_(
                        db.records.c.scope_id == db.links.c.scope_id,
                        db.records.c.kind == db.links.c.kind,
                        db.records.c.id == db.links.c.id,
                        db.records.c.version == db.links.c.version,
                    ),
                )
                .where(
                    db.links.c.scope_id == key, db.links.c.kind == kind, db.links.c.id == record_id
                )
            )
            basis[(str(kind), str(record_id))] = {str(r[0]) for r in rows.all()}
        return basis

    async def dependents(
        self, scope: Scope, source_ids: Sequence[str]
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        await self.store.check_schema()
        gone = set(source_ids)
        sole: list[tuple[str, str]] = []
        shared: list[tuple[str, str]] = []
        async with self.store.connection() as connection:
            basis = await self._links(connection, scope_key(scope), source_ids)
        for (kind, record_id), sources in sorted(basis.items()):
            # Social records keep exact obligations: only the owner may decide on their text.
            if kind in ERASABLE and sources <= gone:
                sole.append((kind, record_id))
            else:
                shared.append((kind, record_id))
        return tuple(sole), tuple(shared)

    async def durable_derivatives(self, scope: Scope, source_id: str) -> int:
        await self.store.check_schema()
        async with self.store.connection() as connection:
            basis = await self._links(connection, scope_key(scope), [source_id])
        return sum(1 for kind, _ in basis if kind in ERASABLE)

    # ---- erasure --------------------------------------------------------------------------

    async def revoke(self, scope: Scope, source_id: str, availability: str) -> bool:
        await self.store.check_schema()
        async with self.store.connection(write=True) as connection:
            result = await connection.execute(
                update(db.records)
                .where(
                    db.records.c.scope_id == scope_key(scope),
                    db.records.c.kind == "Source",
                    db.records.c.id == source_id,
                )
                .values(available=False, deleted=availability == "deleted")
            )
        return bool(result.rowcount)

    async def erase_source(self, scope: Scope, source_id: str, availability: str) -> bool:
        await self.store.check_schema()
        key = scope_key(scope)
        async with self.store.connection(write=True) as connection:
            await self._secure(connection)
            row = (
                await connection.execute(
                    select(db.records.c.version).where(
                        db.records.c.scope_id == key,
                        db.records.c.kind == "Source",
                        db.records.c.id == source_id,
                    )
                )
            ).first()
            if row is None:
                return False
            current = row[0]
            revisions = (
                await connection.execute(
                    select(db.revisions.c.version, db.revisions.c.payload).where(
                        db.revisions.c.scope_id == key,
                        db.revisions.c.kind == "Source",
                        db.revisions.c.id == source_id,
                    )
                )
            ).all()
            for version, payload in revisions:
                if availability == "deleted" and version != current:
                    await connection.execute(
                        delete(db.revisions).where(
                            db.revisions.c.scope_id == key,
                            db.revisions.c.kind == "Source",
                            db.revisions.c.id == source_id,
                            db.revisions.c.version == version,
                        )
                    )
                    continue
                body = json.loads(str(payload))
                body.update(content=None, artifact=None)
                if version == current:
                    body["availability"] = availability
                await connection.execute(
                    update(db.revisions)
                    .where(
                        db.revisions.c.scope_id == key,
                        db.revisions.c.kind == "Source",
                        db.revisions.c.id == source_id,
                        db.revisions.c.version == version,
                    )
                    .values(payload=json.dumps(body, ensure_ascii=False, separators=(",", ":")))
                )
            await connection.execute(
                update(db.records)
                .where(
                    db.records.c.scope_id == key,
                    db.records.c.kind == "Source",
                    db.records.c.id == source_id,
                )
                .values(available=False, deleted=availability == "deleted")
            )
        return True

    async def _erase(
        self, connection: AsyncConnection, key: str, kind: str, record_id: str, *, whole: bool
    ) -> None:
        identity = (
            db.links.c.scope_id == key,
            db.links.c.kind == kind,
            db.links.c.id == record_id,
        )
        await connection.execute(delete(db.links).where(*identity))
        await connection.execute(
            delete(db.revisions).where(
                db.revisions.c.scope_id == key,
                db.revisions.c.kind == kind,
                db.revisions.c.id == record_id,
            )
        )
        await connection.execute(
            delete(db.access).where(
                db.access.c.scope_id == key, db.access.c.kind == kind, db.access.c.id == record_id
            )
        )
        if whole:
            await connection.execute(
                delete(db.keys).where(
                    db.keys.c.scope_id == key, db.keys.c.kind == kind, db.keys.c.id == record_id
                )
            )
            await connection.execute(
                delete(db.records).where(
                    db.records.c.scope_id == key,
                    db.records.c.kind == kind,
                    db.records.c.id == record_id,
                )
            )
        else:
            # Identity stays blocked: the row cannot be revised or re-imported over.
            await connection.execute(
                update(db.records)
                .where(
                    db.records.c.scope_id == key,
                    db.records.c.kind == kind,
                    db.records.c.id == record_id,
                )
                .values(available=False, deleted=True)
            )

    async def erase_records(self, scope: Scope, refs: Sequence[tuple[str, str]]) -> int:
        await self.store.check_schema()
        async with self.store.connection(write=True) as connection:
            await self._secure(connection)
            for kind, record_id in refs:
                await self._erase(connection, scope_key(scope), kind, record_id, whole=False)
        return len(refs)

    # ---- metadata lifecycle ---------------------------------------------------------------

    async def metadata_sweep(
        self,
        scope: Scope,
        *,
        llm_before: datetime,
        aggregate_before: datetime,
        job_before: datetime,
        now: datetime,
    ) -> dict[str, int]:
        await self.store.check_schema()
        key = scope_key(scope)
        counts = dict.fromkeys(
            ("llm_attempts", "cost_calculations", "aggregates", "jobs", "receipts", "outbox"), 0
        )
        async with self.store.connection(write=True) as connection:
            await self._secure(connection)
            await self._llm(connection, key, stamp(llm_before), counts)
            removed = await connection.execute(
                text("DELETE FROM retention_usage_aggregates WHERE scope_id=:k AND month < :m"),
                {"k": key, "m": _month(stamp(aggregate_before))},
            )
            counts["aggregates"] = removed.rowcount
            await self._jobs(connection, key, stamp(job_before), counts)
            cleared = await connection.execute(
                update(db.operations)
                .where(
                    db.operations.c.scope_id == key,
                    db.operations.c.expires_at <= stamp(now),
                    db.operations.c.receipt.is_not(None),
                )
                .values(receipt=None)
            )
            counts["receipts"] = cleared.rowcount
            sent = await connection.execute(
                delete(db.outbox).where(
                    db.outbox.c.scope_id == key,
                    db.outbox.c.status == "delivered",
                    db.outbox.c.created_at <= stamp(job_before),
                )
            )
            counts["outbox"] = sent.rowcount
        if any(counts.values()):
            await self.finalize()
        return counts

    async def _old(
        self, connection: AsyncConnection, key: str, kind: str, before: str
    ) -> list[tuple[str, dict[str, Any], str]]:
        rows = await connection.execute(
            select(db.records.c.id, db.revisions.c.payload, db.records.c.created_at)
            .select_from(
                db.records.join(
                    db.revisions,
                    and_(
                        db.records.c.scope_id == db.revisions.c.scope_id,
                        db.records.c.kind == db.revisions.c.kind,
                        db.records.c.id == db.revisions.c.id,
                        db.records.c.version == db.revisions.c.version,
                    ),
                )
            )
            .where(
                db.records.c.scope_id == key,
                db.records.c.kind == kind,
                db.records.c.created_at < before,
            )
        )
        return [(r[0], json.loads(r[1]), r[2]) for r in rows.all()]

    async def _llm(
        self, connection: AsyncConnection, key: str, before: str, counts: dict[str, int]
    ) -> None:
        attempts = await self._old(connection, key, "LLMAttempt", before)
        # A calculation may be younger than its attempt: it leaves together with the attempt.
        calculations: dict[str, list[tuple[str, dict[str, Any], str]]] = defaultdict(list)
        for record_id, body, created in await self._old(connection, key, "CostCalculation", "9"):
            calculations[body["attempt_id"]].append((record_id, body, created))
        sums: dict[tuple[str, ...], dict[str, Any]] = {}
        for _, body, created in attempts:
            usage = body["usage"]
            calcs = calculations.get(body["attempt_id"], [])
            money = next((c for _, c, _ in calcs if c["amount"] is not None), None)
            bucket = sums.setdefault(
                (
                    _month(created),
                    body["role_id"],
                    body["status"],
                    money["currency"] if money else "-",
                    money["completeness"] if money else "unknown",
                ),
                {
                    "n": 0,
                    "in": 0,
                    "out": 0,
                    "cached": 0,
                    "reason": 0,
                    "unk": 0,
                    "amt": Decimal(0),
                    "unk_amt": 0,
                },
            )
            bucket["n"] += 1
            if usage["completeness"] != "complete":
                bucket["unk"] += 1  # unknown is counted apart, never as zero tokens
            bucket["in"] += usage["input_tokens"] or 0
            bucket["out"] += usage["output_tokens"] or 0
            bucket["cached"] += usage["cached_input_tokens"] or 0
            bucket["reason"] += usage["reasoning_output_tokens"] or 0
            if money is None:
                bucket["unk_amt"] += 1
            else:
                bucket["amt"] += Decimal(money["amount"])
        for (month, role, status, currency, completeness), v in sums.items():
            await connection.execute(
                text(
                    "INSERT INTO retention_usage_aggregates VALUES"
                    " (:k,:m,:role,:s,:c,:q,:n,:i,:o,:ca,:re,:u,:a,:ua)"
                    " ON CONFLICT(scope_id, month, role_id, status, currency, completeness)"
                    " DO UPDATE SET"
                    " attempts=attempts+:n, input_tokens=input_tokens+:i,"
                    " output_tokens=output_tokens+:o,"
                    " cached_input_tokens=cached_input_tokens+:ca,"
                    " reasoning_output_tokens=reasoning_output_tokens+:re,"
                    " unknown_usage=unknown_usage+:u, unknown_amount=unknown_amount+:ua,"
                    " amount=CAST(CAST(amount AS NUMERIC)+CAST(:a AS NUMERIC) AS TEXT)"
                ),
                {
                    "k": key,
                    "m": month,
                    "role": role,
                    "s": status,
                    "c": currency,
                    "q": completeness,
                    "n": v["n"],
                    "i": v["in"],
                    "o": v["out"],
                    "ca": v["cached"],
                    "re": v["reason"],
                    "u": v["unk"],
                    "a": str(v["amt"]),
                    "ua": v["unk_amt"],
                },
            )
        for record_id, _, _ in attempts:
            await self._erase(connection, key, "LLMAttempt", record_id, whole=True)
            counts["llm_attempts"] += 1
        leaving = {body["attempt_id"] for _, body, _ in attempts}
        for attempt_id, items in calculations.items():
            for record_id, _, created in items:
                if attempt_id in leaving or created < before:
                    await self._erase(connection, key, "CostCalculation", record_id, whole=True)
                    counts["cost_calculations"] += 1

    async def _jobs(
        self, connection: AsyncConnection, key: str, before: str, counts: dict[str, int]
    ) -> None:
        for kind in ("Job", "OperationProjection"):
            for record_id, body, _ in await self._old(connection, key, kind, before):
                if body["status"] in TERMINAL:
                    await self._erase(connection, key, kind, record_id, whole=True)
                    counts["jobs"] += 1
        for record_id, body, _ in await self._old(connection, key, "ExecutionResult", before):
            if body["status"] != "OUTCOME_UNKNOWN":
                await self._erase(connection, key, "ExecutionResult", record_id, whole=True)
                counts["jobs"] += 1

    async def aggregates(self, scope: Scope) -> list[dict[str, Any]]:
        async with self.store.connection() as connection:
            rows = await connection.execute(
                text(
                    "SELECT * FROM retention_usage_aggregates WHERE scope_id=:k"
                    " ORDER BY month, role_id, status, currency, completeness"
                ),
                {"k": scope_key(scope)},
            )
            return [dict(r) for r in rows.mappings().all()]

    # ---- physical finish ------------------------------------------------------------------

    async def finalize(self) -> None:
        """Overwrite freed pages, truncate the WAL and compact the file (no open transactions)."""

        def work() -> None:
            raw = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            try:
                raw.execute("PRAGMA secure_delete=ON")
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                raw.execute("VACUUM")
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                raw.close()

        try:
            await asyncio.to_thread(work)
        except sqlite3.OperationalError:
            from ai_agent_contracts.codec import ContractError
            from ai_agent_contracts.states import ErrorCode

            raise ContractError(ErrorCode.RESOURCE_BUSY) from None
