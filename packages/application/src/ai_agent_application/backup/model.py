"""Backup records. A manifest names files, checksums and versions; it never holds content."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Kind = Literal["daily", "pre_migration", "pre_restore", "manual"]
FORMAT = "ai-agent-backup/1"
DATABASE = "database.sqlite"


@dataclass(frozen=True)
class FileEntry:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    format: str
    backup_id: str
    kind: Kind
    created_at: datetime
    schema_head: str
    ledger_id: str
    ledger_seq: int
    files: tuple[FileEntry, ...]
    software: str = ""


@dataclass(frozen=True)
class BackupResult:
    manifest: Manifest
    pruned: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()  # e.g. quota 80% / 90%


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str = ""
    blocking: bool = True


@dataclass(frozen=True)
class RestorePreview:
    backup_id: str
    kind: str
    created_at: datetime
    schema_head: str
    needs_migration: bool
    issues: tuple[Issue, ...]
    scopes: int
    non_terminal_operations: int  # their outcome after the backup is unknown
    pending_outbox: int  # will be quarantined, never replayed
    digest: str

    @property
    def blocked(self) -> bool:
        return any(i.blocking for i in self.issues)


@dataclass(frozen=True)
class RestoreReport:
    backup_id: str
    safety_backup_id: str
    reapplied: tuple[str, ...]
    expired: int
    quarantined_outbox: int
    reindexed: int
    verified: tuple[str, ...]
    agent_state: str = "STOPPED"


@dataclass(frozen=True)
class MigrationReport:
    backup_id: str | None
    migrated: bool
    error: str = ""
    restore_from: str | None = None  # set when the migration failed after the copy was taken
    warnings: tuple[str, ...] = field(default_factory=tuple)
