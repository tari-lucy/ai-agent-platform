"""DerivedPurger for the existing retention owner. No endpoint, rights or delete ledger."""

from collections.abc import Awaitable, Callable, Sequence

from ai_agent_application.retention.ports import DeletionLedger
from ai_agent_contracts.common import Scope
from sqlalchemy import update

from ai_agent_adapters.retention.physical import SQLitePhysicalStore
from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key

from .directory import DirectoryBlobs
from .lifecycle import LifecycleError, PurgeImpact, impact

type ConfirmImpact = Callable[[Scope, PurgeImpact], Awaitable[None]]
type Quiesce = Callable[[Scope, Sequence[str]], Awaitable[None]]


class WorkplacePurger:
    def __init__(
        self,
        store: SQLiteStore,
        blobs: DirectoryBlobs,
        ledger: DeletionLedger,
        *,
        confirm: ConfirmImpact,
        quiesce: Quiesce,
        max_rows: int = 10000,
        max_metadata_bytes: int = 32 * 1024 * 1024,
        max_blobs: int = 20000,
    ) -> None:
        self.store, self.blobs, self.ledger = store, blobs, ledger
        self.confirm, self.quiesce = confirm, quiesce
        self.max_rows, self.max_metadata_bytes, self.max_blobs = (
            max_rows,
            max_metadata_bytes,
            max_blobs,
        )
        if any(type(n) is not int or n < 1 for n in (max_rows, max_metadata_bytes, max_blobs)):
            raise LifecycleError("WORKPLACE_SCAN_INVALID")

    async def preview(self, scope: Scope, source_ids: Sequence[str]) -> PurgeImpact:
        async with self.store.connection() as connection:
            return await impact(
                connection,
                scope,
                source_ids,
                max_rows=self.max_rows,
                max_bytes=self.max_metadata_bytes,
            )

    async def purge_sources(self, scope: Scope, source_ids: Sequence[str]) -> None:
        selected = set(source_ids)
        blocked = {entry.source_id for entry in await self.ledger.entries(scope)}
        if not selected or not selected <= blocked:
            raise LifecycleError("WORKPLACE_SOURCE_NOT_BLOCKED")
        # Hide the entire affected identity/history before waiting on caches or IO.
        # A failed confirmation or file removal must not leave a readable ticket
        # body simply because its current revision dropped a historic material link.
        async with self.store.connection(write=True) as connection:
            initial = await impact(
                connection,
                scope,
                source_ids,
                max_rows=self.max_rows,
                max_bytes=self.max_metadata_bytes,
            )
            for kind, identifier in initial.identities:
                await connection.execute(
                    update(db.records)
                    .where(
                        db.records.c.scope_id == scope_key(scope),
                        db.records.c.kind == kind,
                        db.records.c.id == identifier,
                    )
                    .values(available=False)
                )
        # Stops task/harness caches and waits for a confirmed resource cleanup.
        await self.quiesce(scope, source_ids)
        physical = SQLitePhysicalStore(self.store)
        async with self.store.connection(write=True) as connection:
            plan = await impact(
                connection,
                scope,
                source_ids,
                max_rows=self.max_rows,
                max_bytes=self.max_metadata_bytes,
            )
            # The existing operator deletion preview must include these exact bodies.
            # No permissive default: until host proof wiring lands this module stays off.
            await self.confirm(scope, plan)
            await self.blobs.prune(scope, plan.keep_blobs, max_entries=self.max_blobs)
            await physical.erase_records_locked(connection, scope, plan.identities)
        # RetentionService owns finalize/VACUUM/tombstone/purged_at after all purgers.

    async def reapply_ledger(self, scope: Scope, source_ids: Sequence[str]) -> None:
        """Also remove copies in backups whose source stub was already marked clean."""
        await self.purge_sources(scope, source_ids)
