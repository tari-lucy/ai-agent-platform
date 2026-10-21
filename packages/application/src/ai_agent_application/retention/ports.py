"""Ports of retention: an independent ledger, physical erasure and derived-data purgers."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from ai_agent_contracts.common import Scope

from .model import LedgerEntry, Reason, SourceStub


class DeletionLedger(Protocol):
    """Kept apart from the database and from its backups; IDs and times only."""

    async def block(
        self, scope: Scope, source_id: str, version: int, reason: Reason, at: datetime
    ) -> LedgerEntry:
        """Idempotent. A stricter reason is never weakened by a later softer one."""
        ...

    async def mark_purged(self, scope: Scope, source_id: str, at: datetime) -> None: ...

    async def entries(self, scope: Scope) -> list[LedgerEntry]: ...


class DerivedPurger(Protocol):
    """Caches, exports, contexts, indexes: anything that holds a copy of source text."""

    async def purge_sources(self, scope: Scope, source_ids: Sequence[str]) -> None: ...


@runtime_checkable
class LedgerPurger(Protocol):
    """A restored source stub may be clean while newly supported derivatives are not.

    Optional extension for purgers added after old backups were made. Reconcile
    clean/missing-source entries too; do not trust a historical purged_at alone.
    """

    async def reapply_ledger(self, scope: Scope, source_ids: Sequence[str]) -> None: ...


class PhysicalStore(Protocol):
    async def source(self, scope: Scope, source_id: str) -> SourceStub | None:
        """Includes sources hidden by TTL: only the retention owner may see them."""
        ...

    async def due_sources(
        self, scope: Scope, *, now: datetime, test_cutoff: datetime | None
    ) -> list[SourceStub]: ...

    async def dependents(
        self, scope: Scope, source_ids: Sequence[str]
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        """(sole-basis, shared-basis) derived records as (kind, id)."""
        ...

    async def durable_derivatives(self, scope: Scope, source_id: str) -> int:
        """Episodes/claims/summaries that keep a trace of this source after its text goes."""
        ...

    async def revoke(self, scope: Scope, source_id: str, availability: str) -> bool:
        """Logical block only (flags): from here on nothing is served. Text is erased next."""
        ...

    async def erase_source(self, scope: Scope, source_id: str, availability: str) -> bool:
        """Remove text from the current row and every revision; flip flags in the same unit."""
        ...

    async def erase_records(self, scope: Scope, refs: Sequence[tuple[str, str]]) -> int: ...

    async def metadata_sweep(
        self,
        scope: Scope,
        *,
        llm_before: datetime,
        aggregate_before: datetime,
        job_before: datetime,
        now: datetime,
    ) -> dict[str, int]: ...

    async def finalize(self) -> None:
        """Make the removal physical: overwrite freed pages, empty the WAL, compact the file."""
        ...
