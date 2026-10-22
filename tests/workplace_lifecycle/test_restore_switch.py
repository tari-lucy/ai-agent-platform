"""A failed/torn file switch stays blocked and restores the old blob root on IO failure."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_agent_adapters.backup.sqlite import SQLiteSnapshotter
from ai_agent_adapters.workspace_files.bundle import BlobBundle
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from test_bundle import LIMITS
from test_purge import AREA, MARKER, prepare

from ai_agent_core.local_backup.compose import GuardedSwitcher
from ai_agent_core.local_backup.workplace import STAGED_BLOBS, verify_restore_binding
from ai_agent_core.local_host.layout import HostError


def test_switch_failure_restores_old_blobs_and_cannot_finish_as_target(tmp_path):
    async def scenario():
        w, _, _, _ = await prepare(tmp_path)
        try:
            database = Path(w.store.engine.url.database)
            stage = tmp_path / "stage"
            stage.mkdir(mode=0o700)
            target = stage / "database.sqlite"
            await SQLiteSnapshotter(database).snapshot(target)
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE synthetic_target_marker(value TEXT)")
            blobs = DirectoryBlobs.prepare(stage / STAGED_BLOBS)
            bundle = BlobBundle(LIMITS)
            await bundle.copy(target, w.blobs, blobs)

            class BrokenSwitcher:
                async def switch(self, staged):
                    raise OSError("synthetic disk failure")

            async def verify(db, store):
                await bundle.copy(db, store)

            place = SimpleNamespace(live=w.blobs, bundle=bundle, verify=verify)
            marker = tmp_path / "RESTORE-PENDING"
            wrapper = GuardedSwitcher(BrokenSwitcher(), marker, place)
            with pytest.raises(OSError, match="synthetic disk failure"):
                await wrapper.switch(stage)
            assert marker.is_file()
            import hashlib

            digest = hashlib.sha256(MARKER.encode()).hexdigest()
            assert await w.blobs.read(AREA, digest, 4096) == MARKER.encode()
            with pytest.raises(HostError, match="RESTORE_SWITCH_UNPROVEN"):
                await verify_restore_binding(tmp_path, database)
            # The marker was bound to the selected target, never to the old live DB.
            await verify_restore_binding(tmp_path, target)
        finally:
            await w.close()

    asyncio.run(scenario())
