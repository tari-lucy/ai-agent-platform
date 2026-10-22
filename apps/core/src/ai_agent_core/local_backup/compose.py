"""Thin production composition of the existing BackupService for one local host directory.

No second backup format: the accepted snapshotter, directory store, staging, ledger gate and file
switcher are wired to the host's paths. Nothing here starts the runtime or a model, and nothing
schedules itself: `create_backup` is a callback for the coordinator's daily cycle.
"""

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ai_agent_adapters.backup import (
    DirectoryBackupStore,
    FileSwitcher,
    SQLiteRestoreTarget,
    SQLiteSnapshotter,
    SQLiteStagingFactory,
)
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.retention import SQLitePhysicalStore
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.migrations import HEAD
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_application.backup import (
    BackupPolicy,
    BackupResult,
    BackupService,
    Issue,
    LedgerView,
    Manifest,
)
from ai_agent_application.backup.model import Kind
from ai_agent_application.memory import IndexHealth, MemoryService
from ai_agent_application.retention import DeletionLedger, RetentionService
from ai_agent_application.retention.ports import DerivedPurger
from ai_agent_application.storage import Owner

from ai_agent_core.local_host.layout import (
    HostConfig,
    HostError,
    assignments_fit,
    load_config,
    restrict,
    write_secret,
)
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

from .compat import HostCompatibility
from .layout import HOST_CONFIG, BackupLayout
from .ledger import open_existing_ledger
from .workplace import STAGED_BLOBS, WorkplaceRestore, database_digest

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


class GuardedSwitcher:
    """A database switch alone cannot admit an old derived directory to serve."""

    def __init__(
        self, switcher: FileSwitcher, marker: Path, workplace: WorkplaceRestore | None = None
    ) -> None:
        self.switcher, self.marker = switcher, marker
        self.workplace = workplace

    async def switch(self, staged_dir: Path) -> None:
        if self.workplace is not None:
            await self.workplace.verify(
                staged_dir / "database.sqlite", DirectoryBlobs(staged_dir / STAGED_BLOBS)
            )
        binding = await asyncio.to_thread(database_digest, staged_dir / "database.sqlite")
        write_secret(self.marker, json.dumps({"database_digest": binding}) + "\n")
        place = self.workplace
        if place is None or place.live is None:
            await self.switcher.switch(staged_dir)
            return
        database = staged_dir / "database.sqlite"
        incoming = DirectoryBlobs.prepare(place.live.root.with_name(".restore-" + uuid4().hex))
        await place.bundle.copy(database, DirectoryBlobs(staged_dir / STAGED_BLOBS), incoming)
        # Adjacent roots keep renames on the same filesystem even with external blob storage.
        kept = place.live.root.with_name(".replaced-" + uuid4().hex)
        displaced = False
        try:
            os.replace(place.live.root, kept)
            displaced = True
            os.replace(incoming.root, place.live.root)
            await self.switcher.switch(staged_dir)
        except BaseException:
            if displaced:
                if place.live.root.exists():
                    os.replace(place.live.root, incoming.root)
                os.replace(kept, place.live.root)
            raise
        # Only the explicit finish-restore gate removes this marker, even after an IO failure.


class HostConfigFile:
    """The one allowed extra file: a private, consistent copy of the nonsecret host.json."""

    def __init__(self, layout: BackupLayout) -> None:
        self.layout = layout

    def files(self) -> list[tuple[str, Path]]:
        config = load_config(self.layout.host)  # validated and size bounded; never raw bytes
        directory = self.layout.work_dir / "extra"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        restrict(directory, directory=True)
        handle, name = tempfile.mkstemp(dir=directory, prefix=".host-", suffix=".tmp")
        restrict(Path(name), directory=False)
        target = directory / HOST_CONFIG
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(config.model_dump_json(indent=1) + "\n")
        os.replace(name, target)
        return [(HOST_CONFIG, target)]


class BackupLedger(LedgerView, DeletionLedger, Protocol):
    """What the service needs of the ledger: its identity for copies and the deletion history."""


class OwnedLedger(BackupLedger, Protocol):
    def close(self) -> None: ...


class LocalBackup:
    """The composed service plus the pieces a caller needs to release."""

    def __init__(
        self, service: BackupService, gate: HostLockGate, owned_ledger: OwnedLedger | None
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
    if policy is None and config.recovery is not None:
        policy = BackupPolicy(
            keep_daily=config.recovery.keep_daily, quota_bytes=config.recovery.backup_quota_bytes
        )
    owned: OwnedLedger | None = None
    if ledger is None:
        if config.recovery is not None:
            from ai_agent_core.local_host.recovery import HostLedger

            binding = HostLedger(host, config)
            if binding.place != layout:
                binding.close()
                raise HostError("RECOVERY_BINDING_DIFFERS")
            owned = binding
        else:
            owned = open_existing_ledger(layout)
        ledger = owned
    layout.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict(layout.work_dir, directory=True)
    registry = new_registry()
    reader = PRINCIPALS[Owner.MEMORY]
    gate = HostLockGate()
    workplace = WorkplaceRestore(layout, config, ledger, gate) if config.recovery else None

    async def build(database: Path) -> SQLiteRestoreTarget:
        """The staged copy opened like the host opens its own: same extensions and recall index."""
        staged = SQLiteStore(database, extensions=registry)
        index = SQLiteRecallIndex(database.parent / "index.sqlite")  # FTS only, as in serve
        memory = MemoryService(staged, reader, registry, index=index, health=IndexHealth())
        physical = SQLitePhysicalStore(staged)
        purgers: tuple[DerivedPurger, ...] = ()
        if workplace is not None:
            root = database.parent / STAGED_BLOBS
            blobs = DirectoryBlobs(root) if root.exists() else DirectoryBlobs.prepare(root)
            purgers = (workplace.purger(staged, blobs),)
        retention = RetentionService(
            staged, reader, memory, physical, ledger, index=index, purgers=purgers
        )
        return SQLiteRestoreTarget(staged, retention, memory, physical, index.close)

    async def close_live() -> None:
        """Offline restore: this process opened no live store, and the host lock is held."""

    compatible = HostCompatibility(
        host, config, assignments_fit=assignments_fit, workplace_ready=workplace is not None
    )

    async def restore_check(manifest: Manifest, copy_dir: Path) -> list[Issue]:
        issues = await compatible(manifest, copy_dir)
        if workplace is not None and not any(issue.blocking for issue in issues):
            issues.extend(await workplace.preview(manifest, copy_dir))
        return issues

    switcher = FileSwitcher(host.database, host.dialogue_index, layout.replaced_dir)
    service = BackupService(
        SQLiteSnapshotter(host.database),
        DirectoryBackupStore(layout.store_dir),
        ledger,
        gate,
        SQLiteStagingFactory(build),
        GuardedSwitcher(switcher, host.root / "RESTORE-PENDING", workplace)
        if config.recovery is not None
        else switcher,
        work_dir=layout.work_dir,
        current_head=HEAD,
        close_live=close_live,
        ledger_dir=layout.ledger_copies,
        extra=HostConfigFile(layout),
        restore_check=restore_check,
        snapshot_extra=workplace.snapshot if workplace else None,
        stage_files=workplace.prepare if workplace else None,
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
