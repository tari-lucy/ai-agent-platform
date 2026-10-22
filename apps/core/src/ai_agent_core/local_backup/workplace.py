"""Offline workplace restore using the existing bundle, deletion ledger and owners.

Nothing here starts a runtime, grants rights or replays a runner receipt.
"""

import asyncio
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.workspace_files.bundle import (
    BUNDLE_NAME,
    BlobBundle,
    BundleLimits,
    WorkplaceSnapshotFiles,
)
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_adapters.workspace_files.lifecycle import LifecycleError
from ai_agent_adapters.workspace_files.purge import WorkplacePurger
from ai_agent_adapters.workspace_files.safe_read import UnsafeBlobError
from ai_agent_application.agent_activity.sessions import SessionBook
from ai_agent_application.backup import Issue, Manifest
from ai_agent_application.backup.model import DATABASE
from ai_agent_application.memory import derived_id
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.common import Scope, SourceRef

from ai_agent_core.local_host.layout import HostConfig, HostError, restrict
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

from .layout import BackupLayout

if TYPE_CHECKING:
    from .compose import BackupLedger, HostLockGate

STAGED_BLOBS = "workplace-blobs"


def database_digest(database: Path) -> str:
    """Logical contents, stable across SQLite WAL/checkpoint/header changes."""
    digest = hashlib.sha256()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        for line in connection.iterdump():
            digest.update(line.encode())
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


class WorkplaceRestore:
    def __init__(
        self, layout: BackupLayout, config: HostConfig, ledger: BackupLedger, gate: HostLockGate
    ) -> None:
        self.layout, self.config, self.ledger, self.gate = layout, config, ledger, gate
        settings = config.workplace_files
        self.live = DirectoryBlobs(Path(settings.blob_dir)) if settings else None
        scopes = len(config.test_space_ids) + 1 + int(config.settings_space_id is not None)
        quota = config.recovery.backup_quota_bytes if config.recovery else 1073741824
        self.limits = BundleLimits(
            settings.max_file_bytes if settings else 1048576,
            min(quota, 2**31 - 1),
            min(settings.max_versions * scopes, 60000) if settings else 20000,
            settings.max_versions * scopes if settings else 20000,
            32 * 1024 * 1024,
        )
        self.bundle = BlobBundle(self.limits)

    @property
    def snapshot(self) -> WorkplaceSnapshotFiles | None:
        return WorkplaceSnapshotFiles(self.live, self.limits) if self.live else None

    def offline(self, database: Path) -> None:
        if not self.gate.held or database.parent.parent != self.layout.work_dir:
            raise HostError("WORKPLACE_RESTORE_OFFLINE_REQUIRED")

    def purger(
        self, store: SQLiteStore, blobs: DirectoryBlobs, *, finishing: bool = False
    ) -> WorkplacePurger:
        database = Path(store.engine.url.database or "")

        def permitted() -> None:
            if finishing:
                if (
                    not self.gate.held
                    or database != self.layout.host.database
                    or not (self.layout.host.root / "RESTORE-PENDING").is_file()
                ):
                    raise HostError("WORKPLACE_RESTORE_OFFLINE_REQUIRED")
            else:
                self.offline(database)

        async def confirm(scope: Scope, plan: object) -> None:
            # Only the private stage of an explicitly previewed restore is writable here.
            permitted()

        async def quiesce(scope: Scope, source_ids: object) -> None:
            permitted()  # host lock proves there are no runtime/runner caches

        return WorkplacePurger(
            store,
            blobs,
            self.ledger,
            confirm=confirm,
            quiesce=quiesce,
            max_rows=self.limits.max_revisions,
            max_metadata_bytes=self.limits.max_metadata_bytes,
            max_blobs=self.limits.max_entries,
        )

    async def prepare(self, database: Path, copy_dir: Path) -> None:
        self.offline(database)
        blobs = DirectoryBlobs.prepare(database.parent / STAGED_BLOBS)
        if self.live is not None:
            await self.bundle.restore(database, copy_dir / BUNDLE_NAME, blobs)
        store = SQLiteStore(database, extensions=new_registry())

        def authorize(scope: Scope) -> None:
            self.offline(database)

        async def unused(scope: Scope, ref: SourceRef, text: str) -> None:
            raise HostError("RESTORE_CANNOT_DELIVER")

        owner = SessionBook(
            store,
            PRINCIPALS[Owner.ACTIVITY],
            lambda: datetime.now(UTC),
            authorize=authorize,
            verify_source=unused,
        )
        try:
            # Existing owner performs CAS/replay, never writes raw session state in SQL.
            async with store.connection() as connection:
                from sqlalchemy import text

                rows: Sequence[str] = (
                    (
                        await connection.execute(
                            text(
                                "SELECT v.payload FROM records r JOIN record_revisions v "
                                "USING(scope_id,kind,id,version) WHERE r.kind='WorkSession' "
                                "AND r.available=1 AND r.deleted=0 LIMIT 10001"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            if len(rows) > 10000:
                raise HostError("WORKPLACE_SCAN_LIMIT")
            for payload in rows:
                session = WorkSession.model_validate_json(payload)
                await owner.reset_context(
                    session.scope,
                    session.id,
                    derived_id("restore-context", session.scope, session.id, str(session.version)),
                    expected_version=session.version,
                )
        finally:
            await store.close()

    async def preview(self, manifest: Manifest, copy_dir: Path) -> list[Issue]:
        if self.live is not None and BUNDLE_NAME not in {f.name for f in manifest.files}:
            return [Issue("WORKPLACE_BUNDLE_MISSING")]
        with tempfile.TemporaryDirectory(
            dir=self.layout.work_dir, prefix="workplace-preview-"
        ) as d:
            root = Path(d)
            restrict(root, directory=True)
            blobs = DirectoryBlobs.prepare(root / STAGED_BLOBS)
            database = root / DATABASE
            shutil.copyfile(copy_dir / DATABASE, database)
            try:
                if self.live is not None:
                    await self.bundle.restore(database, copy_dir / BUNDLE_NAME, blobs)
                else:
                    await self.bundle.copy(database, blobs)
                store = SQLiteStore(database, extensions=new_registry())
                try:
                    count, digest = await self.redactions(store, blobs)
                finally:
                    await store.close()
            except LifecycleError, UnsafeBlobError, OSError, ValueError:
                return [Issue("WORKPLACE_BUNDLE_INVALID")]
        return [Issue("WORKPLACE_LEDGER_REDACTIONS", f"{count}:{digest}", blocking=False)]

    async def redactions(self, store: SQLiteStore, blobs: DirectoryBlobs) -> tuple[int, str]:
        from ai_agent_core.local_host.finish_restore import host_scopes

        identity = await self.ledger.identity()
        fingerprints = []
        identities: set[tuple[tuple[str, ...], str, str]] = set()
        purger = self.purger(store, blobs)
        for scope in host_scopes(self.config):
            ids = [entry.source_id for entry in await self.ledger.entries(scope)]
            for start in range(0, len(ids), 500):
                plan = await purger.preview(scope, ids[start : start + 500])
                identities.update((scope.partition, kind, key) for kind, key, _ in plan.records)
                fingerprints.append(plan.digest)
        digest = hashlib.sha256(json.dumps([identity, fingerprints]).encode()).hexdigest()
        return len(identities), digest

    async def verify(self, database: Path, blobs: DirectoryBlobs) -> None:
        await self.bundle.copy(database, blobs)


async def verify_restore_binding(root: Path, database: Path) -> None:
    """A torn multi-file switch must not be certified by finish-restore."""
    from ai_agent_core.local_host.recovery import private

    try:
        path = root / "RESTORE-PENDING"
        private(path)
        with path.open(encoding="utf-8") as stream:
            marker = json.loads(stream.read(1024))
        expected = marker["database_digest"]
    except ValueError, KeyError, TypeError:
        raise HostError("RESTORE_SWITCH_UNPROVEN") from None
    if expected != await asyncio.to_thread(database_digest, database):
        raise HostError("RESTORE_SWITCH_UNPROVEN")
