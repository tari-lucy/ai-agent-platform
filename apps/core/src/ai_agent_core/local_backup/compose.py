"""Thin production composition of the existing BackupService for one local host directory.

No second backup format: the accepted snapshotter, directory store, staging, ledger gate and file
switcher are wired to the host's paths. Nothing here starts the runtime or a model, and nothing
schedules itself: `create_backup` is a callback for the coordinator's daily cycle.
"""

import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ai_agent_adapters.backup import (
    DirectoryBackupStore,
    FileSwitcher,
    SQLiteRestoreTarget,
    SQLiteSnapshotter,
    SQLiteStagingFactory,
)
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.migrations import HEAD
from ai_agent_application.backup import (
    BackupPolicy,
    BackupResult,
    BackupService,
    LedgerView,
)
from ai_agent_application.backup.model import Kind
from ai_agent_application.memory import IndexHealth, MemoryService
from ai_agent_application.retention import DeletionLedger, RetentionService
from ai_agent_application.storage import Owner

from ai_agent_core.local_host.layout import HostConfig, HostError, load_config
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

from .compat import HostCompatibility
from .layout import HOST_CONFIG, BackupLayout
from .ledger import open_existing_ledger

SOFTWARE = "local-backup/1"


class HostLockGate:
    """STOPPED only while this process holds the host's exclusive OS lock.

    A stored RunSession says what the last process believed; only the lock proves that no `serve`
    of this host is alive right now, so the stored record is never consulted.
    """

    def __init__(self) -> None:
        self.held = False

    async def run_state(self) -> str:
        return "STOPPED" if self.held else "UNKNOWN"


class HostConfigFile:
    """The one allowed extra file: a private, consistent copy of the nonsecret host.json."""

    def __init__(self, layout: BackupLayout) -> None:
        self.layout = layout

    def files(self) -> list[tuple[str, Path]]:
        config = load_config(self.layout.host)  # validated and size bounded; never raw bytes
        directory = self.layout.work_dir / "extra"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, name = tempfile.mkstemp(dir=directory, prefix=".host-", suffix=".tmp")
        target = directory / HOST_CONFIG
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(config.model_dump_json(indent=1) + "\n")
        os.replace(name, target)
        return [(HOST_CONFIG, target)]


class BackupLedger(LedgerView, DeletionLedger, Protocol):
    """What the service needs of the ledger: its identity for copies and the deletion history."""


class LocalBackup:
    """The composed service plus the pieces a caller needs to release."""

    def __init__(
        self, service: BackupService, gate: HostLockGate, owned_ledger: SQLiteDeletionLedger | None
    ) -> None:
        self.service, self.gate, self._owned = service, gate, owned_ledger

    def close(self) -> None:
        if self._owned is not None:
            self._owned.close()


def compose(
    layout: BackupLayout,
    config: HostConfig,
    *,
    ledger: BackupLedger | None = None,
    policy: BackupPolicy | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LocalBackup:
    """Build the service. An existing, valid ledger is required; a missing one is never created."""
    host = layout.host
    if not host.database.is_file():
        raise HostError("INIT_INCOMPLETE")
    owned: SQLiteDeletionLedger | None = None
    if ledger is None:
        owned = open_existing_ledger(layout)
        ledger = owned
    layout.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = new_registry()
    reader = PRINCIPALS[Owner.MEMORY]

    async def build(database: Path) -> SQLiteRestoreTarget:
        """The staged copy opened like the host opens its own: same extensions and recall index."""
        staged = SQLiteStore(database, extensions=registry)
        index = SQLiteRecallIndex(database.parent / "index.sqlite")  # FTS only, as in serve
        memory = MemoryService(staged, reader, registry, index=index, health=IndexHealth())
        physical = SQLitePhysicalStore(staged)
        retention = RetentionService(staged, reader, memory, physical, ledger, index=index)
        return SQLiteRestoreTarget(staged, retention, memory, physical, index.close)

    async def close_live() -> None:
        """Offline restore: this process opened no live store, and the host lock is held."""

    gate = HostLockGate()
    service = BackupService(
        SQLiteSnapshotter(host.database),
        DirectoryBackupStore(layout.store_dir),
        ledger,
        gate,
        SQLiteStagingFactory(build),
        FileSwitcher(host.database, host.dialogue_index, layout.replaced_dir),
        work_dir=layout.work_dir,
        current_head=HEAD,
        close_live=close_live,
        ledger_dir=layout.ledger_copies,
        extra=HostConfigFile(layout),
        restore_check=HostCompatibility(host, config),
        policy=policy,
        clock=clock,
        software=SOFTWARE,
    )
    return LocalBackup(service, gate, owned)


def create_backup_callback(
    layout: BackupLayout,
    *,
    ledger: BackupLedger | None = None,
    policy: BackupPolicy | None = None,
    kind: Kind = "daily",
) -> Callable[[], Awaitable[BackupResult]]:
    """Async callback for the coordinator's daily cycle.

    It takes one consistent snapshot (SQLite backup API, safe beside a running host) and applies
    the existing BackupPolicy. It does not start the runtime or a model and has no schedule of its
    own. Pass the retention owner's own ledger once it is bound; otherwise an existing ledger file
    is opened for the call and closed again.
    """

    async def create() -> BackupResult:
        config = load_config(layout.host)
        local = compose(layout, config, ledger=ledger, policy=policy)
        try:
            return await local.service.create(kind)
        finally:
            local.close()

    return create
