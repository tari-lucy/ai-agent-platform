"""Explicit offline recovery preparation and anchored access to the independent ledger."""

import asyncio
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.retention import LedgerEntry
from ai_agent_application.retention.model import Reason
from ai_agent_contracts.common import Scope

from ai_agent_core.local_backup.layout import BackupLayout, layout
from ai_agent_core.local_backup.ledger import open_existing_ledger
from ai_agent_core.m3_composition import new_registry

from .layout import (
    HostConfig,
    HostError,
    HostPaths,
    RecoveryConfig,
    load_config,
    paths,
    private_dir,
    restrict,
    save_config,
    windows_private,
    write_secret,
)
from .process import exclusive_host


def no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise HostError("RECOVERY_PATH_UNSAFE")


def private(path: Path) -> None:
    no_links(path)
    try:
        info = path.stat()
        if sys.platform == "win32":
            valid = windows_private(path, parents=0)
        else:
            valid = info.st_uid == os.getuid() and not info.st_mode & 0o077
        if not valid or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise HostError("RECOVERY_PERMISSIONS")
    except OSError:
        raise HostError("RECOVERY_PERMISSIONS") from None


def configured_layout(host: HostPaths, config: HostConfig) -> BackupLayout:
    recovery = config.recovery
    if recovery is None:
        raise HostError("RECOVERY_NOT_ENABLED")
    place = layout(host.root, Path(recovery.store_dir), Path(recovery.ledger_path))
    for item in (
        host.root,
        place.store_dir,
        place.ledger_path,
        place.ledger_path.parent,
        place.ledger_copies,
    ):
        private(item)
    return place


async def enable_recovery(root: Path, *, store_dir: Path, ledger_path: Path) -> None:
    """One-time preparation. Never replace an existing ledger or silently adopt old deletions."""
    host = paths(root)
    with exclusive_host(host.root):
        config = load_config(host)
        private(host.root)
        if config.recovery is not None:
            raise HostError("RECOVERY_ALREADY_ENABLED")
        place = layout(root, store_dir, ledger_path)
        for item in (root, store_dir, ledger_path):
            no_links(item)
        if store_dir.exists() or ledger_path.parent.exists():
            raise HostError("RECOVERY_NEW_DIRECTORIES_REQUIRED")
        # A legacy host with deletions needs an explicit history migration, never an empty ledger.
        connection = sqlite3.connect(f"{host.database.as_uri()}?mode=ro", uri=True)
        try:
            old = connection.execute(
                "SELECT COUNT(*) FROM records r JOIN record_revisions v ON "
                "r.scope_id=v.scope_id AND r.kind=v.kind AND r.id=v.id AND r.version=v.version "
                "WHERE r.kind='DeletionTombstone' OR (r.kind='Source' AND "
                "COALESCE(json_extract(v.payload,'$.availability'),'available')!='available')"
            ).fetchone()[0]
        finally:
            connection.close()
        if old:
            raise HostError("RECOVERY_HISTORY_REQUIRED")
        marker = host.root / "RECOVERY-ENABLE-PENDING"
        write_secret(marker, "recovery preparation incomplete\n")
        private_dir(store_dir)
        private_dir(ledger_path.parent)
        private_dir(place.ledger_copies)
        ledger = SQLiteDeletionLedger(ledger_path)
        try:
            identity, sequence = await ledger.identity()
        finally:
            ledger.close()
        restrict(ledger_path, directory=False)
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        try:
            await store.check_schema()
            await SQLitePhysicalStore(store).install()
        finally:
            await store.close()
        recovery = RecoveryConfig(
            store_dir=str(store_dir),
            ledger_path=str(ledger_path),
            ledger_id=identity,
            ledger_floor=sequence,
        )
        save_config(host, config.model_copy(update={"recovery": recovery}))
        marker.unlink()  # config is committed last; an interrupted preparation stays closed


class HostLedger:
    """Reserve a sequence floor BEFORE a ledger write; a crash cannot acknowledge a rollback.

    A torn reservation can require operator recovery (availability loss), but can never turn a
    lost deletion into permission to read it. The host config is not restored from a data copy.
    """

    def __init__(
        self,
        host: HostPaths,
        config: HostConfig,
        *,
        fail: Callable[[], None] = lambda: None,
    ) -> None:
        self.host = host
        self.place = configured_layout(host, config)
        self.raw = open_existing_ledger(self.place)
        self.fail = fail
        self.lock = asyncio.Lock()
        self.failed = False

    async def _identity(self) -> tuple[str, int]:
        if self.failed:
            raise HostError("LEDGER_UNAVAILABLE")
        recovery = load_config(self.host).recovery
        identity, sequence = await self.raw.identity()
        if recovery is None or identity != recovery.ledger_id or sequence < recovery.ledger_floor:
            self.failed = True
            self.fail()
            raise HostError("LEDGER_ROLLED_BACK")
        return identity, sequence

    async def identity(self) -> tuple[str, int]:
        async with self.lock:
            return await self._identity()

    async def _reserve(self) -> None:
        _, sequence = await self._identity()
        config = load_config(self.host)
        assert config.recovery is not None
        save_config(
            self.host,
            config.model_copy(
                update={
                    "recovery": config.recovery.model_copy(update={"ledger_floor": sequence + 1}),
                }
            ),
        )

    async def block(
        self,
        scope: Scope,
        source_id: str,
        version: int,
        reason: Reason,
        at: datetime,
    ) -> LedgerEntry:
        async with self.lock:
            try:
                await self._reserve()
                return await self.raw.block(scope, source_id, version, reason, at)
            except Exception:
                self.failed = True
                self.fail()
                raise

    async def mark_purged(self, scope: Scope, source_id: str, at: datetime) -> None:
        async with self.lock:
            try:
                await self._reserve()
                await self.raw.mark_purged(scope, source_id, at)
            except Exception:
                self.failed = True
                self.fail()
                raise

    async def entries(self, scope: Scope) -> list[LedgerEntry]:
        async with self.lock:
            await self._identity()
            return await self.raw.entries(scope)

    async def snapshot_to(self, target: Path) -> None:
        async with self.lock:
            await self._identity()
            await self.raw.snapshot_to(target)

    def close(self) -> None:
        self.raw.close()
