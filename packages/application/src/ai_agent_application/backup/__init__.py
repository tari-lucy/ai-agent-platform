"""Backup, restore gate and migration guard (M5-02). Not wired into the runtime."""

from .model import (
    BackupResult,
    FileEntry,
    Issue,
    Manifest,
    MigrationReport,
    RestorePreview,
    RestoreReport,
)
from .policy import BackupPolicy
from .ports import (
    AgentGate,
    BackupStore,
    ExtraFiles,
    LedgerView,
    RestoreTarget,
    Snapshotter,
    StagingFactory,
    Switcher,
)
from .service import BackupService, sha256_file

__all__ = [
    "AgentGate",
    "BackupPolicy",
    "BackupResult",
    "BackupService",
    "BackupStore",
    "ExtraFiles",
    "FileEntry",
    "Issue",
    "LedgerView",
    "Manifest",
    "MigrationReport",
    "RestorePreview",
    "RestoreReport",
    "RestoreTarget",
    "Snapshotter",
    "StagingFactory",
    "Switcher",
    "sha256_file",
]
