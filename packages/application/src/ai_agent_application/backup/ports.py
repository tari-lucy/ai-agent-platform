"""Ports of backup/restore. SQLite details stay in the adapter."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ai_agent_contracts.common import Scope

from ai_agent_application.retention import LedgerApplication, SweepReport

from .model import Kind, Manifest


class Snapshotter(Protocol):
    async def snapshot(self, target: Path) -> None:
        """Consistent copy of the live database taken while it is in use (not a file copy)."""
        ...

    async def head(self, database: Path) -> str: ...

    async def integrity(self, database: Path) -> list[str]: ...


class LedgerView(Protocol):
    async def identity(self) -> tuple[str, int]:
        """(ledger id, change counter): a lost or rolled-back ledger is detectable."""
        ...

    async def snapshot_to(self, target: Path) -> None:
        """Consistent copy for a separate, restricted place (never inside a data backup)."""
        ...


class BackupStore(Protocol):
    """Where copies live. Copies are written whole or not at all."""

    def free_bytes(self) -> int: ...

    def used_bytes(self) -> int: ...

    async def write(
        self, backup_id: str, kind: Kind, files: Sequence[tuple[str, Path]], manifest: Manifest
    ) -> None: ...

    async def manifests(self) -> list[Manifest]: ...

    async def path(self, backup_id: str, name: str) -> Path: ...

    async def remove(self, backup_id: str) -> None: ...


class ExtraFiles(Protocol):
    """Permitted profile/archive snapshots that live outside the database (never `.env`)."""

    def files(self) -> list[tuple[str, Path]]: ...


class SnapshotExtraFiles(Protocol):
    """Build bounded extras from THIS database snapshot, never from the live head.

    Outputs belong in the snapshot's private staging directory. Failure aborts
    the whole copy; the caller removes staging after the store has copied it.
    """

    async def files_for_snapshot(self, database: Path) -> list[tuple[str, Path]]: ...


class AgentGate(Protocol):
    async def run_state(self) -> str:
        """Authoritative run state of the active workspace; restore needs ``STOPPED``."""
        ...


class RestoreTarget(Protocol):
    """A restored database opened in a staging place, with its own rebuildable index."""

    async def scopes(self) -> list[Scope]: ...

    async def apply_ledger(self, scope: Scope) -> LedgerApplication: ...

    async def sweep(self, scope: Scope) -> SweepReport: ...

    async def rebuild_index(self, scope: Scope) -> int: ...

    async def quarantine_outbox(self) -> int: ...

    async def operations_at_risk(self) -> int: ...

    async def pending_outbox(self) -> int: ...

    async def blocked_text_remaining(self, scope: Scope) -> list[str]:
        """Ledger-blocked sources that still hold text. Must be empty before any switch."""
        ...

    async def close(self) -> None: ...


class StagingFactory(Protocol):
    async def open(self, database: Path) -> RestoreTarget: ...


class Switcher(Protocol):
    async def switch(self, staged_dir: Path) -> None:
        """Replace the live database and index with the staged ones. The live store must be
        closed first; the replaced files are kept aside, never deleted here."""
        ...
