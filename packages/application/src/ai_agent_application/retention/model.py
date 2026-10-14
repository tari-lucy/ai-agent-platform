"""Plain results of retention; none of them carries removed content."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Reason = Literal["operator_delete", "expired", "rights_revoked"]


@dataclass(frozen=True)
class LedgerEntry:
    """Minimum kept outside backups: identity, version, reason, times. No content."""

    source_id: str
    version: int
    reason: Reason
    blocked_at: datetime
    purged_at: datetime | None


@dataclass(frozen=True)
class SourceStub:
    """What the physical store knows about a Source without exposing its text."""

    source_id: str
    version: int
    availability: str
    expires_at: datetime | None
    updated_at: datetime
    protected: bool
    has_content: bool


@dataclass(frozen=True)
class DeletionPreview:
    """The exact area of a deletion, shown before anything is changed."""

    source_ids: tuple[str, ...]
    erased_with: tuple[tuple[str, str], ...]  # derived (kind, id) that lose their only basis
    detached: tuple[tuple[str, str], ...]  # derived (kind, id) that keep other sources
    indexed: tuple[str, ...]  # sources present in the derived index
    digest: str


@dataclass(frozen=True)
class SourceOutcome:
    source_id: str
    state: Literal["purged", "blocked_pending_purge"]
    failed_step: str | None = None


@dataclass(frozen=True)
class DeletionReport:
    outcomes: tuple[SourceOutcome, ...]
    erased: tuple[tuple[str, str], ...]
    complete: bool


@dataclass(frozen=True)
class SweepReport:
    expired: tuple[str, ...] = ()
    unconsolidated: tuple[
        str, ...
    ] = ()  # raw text left with no durable fragment: visible, not extended
    pending: tuple[str, ...] = ()
    metadata: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerApplication:
    reapplied: tuple[str, ...]
    missing: tuple[str, ...]  # ledger knows them, the restored store does not
    pending: tuple[str, ...]
