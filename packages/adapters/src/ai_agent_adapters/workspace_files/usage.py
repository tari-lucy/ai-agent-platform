"""Quota metadata counts from the existing catalogue, including hidden/old records.

Called under the catalogue writer reservation: concurrent writers cannot both
admit the last slot. This is a projection, not another registry or migration.
"""

from ai_agent_contracts.common import Scope
from sqlalchemy import func, select

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key


class CatalogueUsage:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def counts(self, scope: Scope) -> tuple[int, int]:
        async with self.store.connection() as connection:
            files = await connection.scalar(
                select(func.count())
                .select_from(db.records)
                .where(
                    db.records.c.scope_id == scope_key(scope),
                    db.records.c.kind == "ManagedFile",
                )
            )
            versions = await connection.scalar(
                select(func.count())
                .select_from(db.revisions)
                .where(
                    db.revisions.c.scope_id == scope_key(scope),
                    db.revisions.c.kind == "ManagedFile",
                )
            )
            return int(files or 0), int(versions or 0)
