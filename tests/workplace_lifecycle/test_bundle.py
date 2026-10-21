"""Only referenced blobs enter a bounded backup extra; no arbitrary zip extraction."""

import asyncio
import hashlib
import zipfile
from pathlib import Path

import pytest
from ai_agent_adapters.backup.sqlite import SQLiteSnapshotter
from ai_agent_adapters.workspace_files.bundle import BUNDLE_NAME, BlobBundle, BundleLimits
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_adapters.workspace_files.lifecycle import LifecycleError
from test_purge import AREA, MARKER, prepare

LIMITS = BundleLimits(4096, 65536, 20, 100, 1024 * 1024)


def test_snapshot_bundle_roundtrip_contains_only_referenced_revisions(tmp_path):
    async def scenario():
        w, _, _, _ = await prepare(tmp_path)
        try:
            snapshot = tmp_path / "snapshot.sqlite"
            await SQLiteSnapshotter(Path(w.store.engine.url.database)).snapshot(snapshot)
            target = tmp_path / BUNDLE_NAME
            bundle = BlobBundle(LIMITS)
            await bundle.create(snapshot, w.blobs, target)
            with zipfile.ZipFile(target) as archive:
                assert len(archive.infolist()) == 3  # old/current note + independent, not orphan
                assert all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())
                assert all(len(item.filename) == 129 for item in archive.infolist())
            restored = DirectoryBlobs.prepare(tmp_path / "restored")
            await bundle.restore(snapshot, target, restored)
            digest = hashlib.sha256(MARKER.encode()).hexdigest()
            assert await restored.read(AREA, digest, 4096) == MARKER.encode()
            with pytest.raises(LifecycleError, match="WORKPLACE_RESTORE_NOT_EMPTY"):
                await bundle.restore(snapshot, target, restored)
            original = target.read_bytes()
            with pytest.raises(LifecycleError):
                await bundle.create(snapshot, w.blobs, target)
            assert target.read_bytes() == original
        finally:
            await w.close()

    asyncio.run(scenario())


def test_missing_snapshot_blob_fails_without_a_partial_backup(tmp_path):
    async def scenario():
        w, _, _, _ = await prepare(tmp_path)
        try:
            snapshot = tmp_path / "snapshot.sqlite"
            await SQLiteSnapshotter(Path(w.store.engine.url.database)).snapshot(snapshot)
            digest = hashlib.sha256(MARKER.encode()).hexdigest()
            (w.blobs._scope(AREA) / digest).unlink()  # simulate purge after the SQLite snapshot
            target = tmp_path / BUNDLE_NAME
            with pytest.raises(LifecycleError):
                await BlobBundle(LIMITS).create(snapshot, w.blobs, target)
            assert not target.exists()
        finally:
            await w.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["traversal", "duplicate", "compression"])
def test_untrusted_bundle_paths_and_formats_do_not_escape_staging(tmp_path, kind):
    async def scenario():
        w, _, _, _ = await prepare(tmp_path)
        try:
            snapshot = tmp_path / "snapshot.sqlite"
            await SQLiteSnapshotter(Path(w.store.engine.url.database)).snapshot(snapshot)
            bundle = BlobBundle(LIMITS)
            target = tmp_path / BUNDLE_NAME
            await bundle.create(snapshot, w.blobs, target)
            with zipfile.ZipFile(target) as archive:
                contents = [(item.filename, archive.read(item)) for item in archive.infolist()]
            neighbor = tmp_path / "neighbor.txt"
            neighbor.write_bytes(b"unrelated")
            if kind == "traversal":
                contents = [("../neighbor.txt", b"forbidden")]
            if kind == "duplicate":
                contents.append(contents[0])
            compression = zipfile.ZIP_DEFLATED if kind == "compression" else zipfile.ZIP_STORED
            with zipfile.ZipFile(target, "w", compression=compression) as archive:
                for name, data in contents:
                    if kind == "duplicate" and name in archive.namelist():
                        with pytest.warns(UserWarning):
                            archive.writestr(name, data)
                    else:
                        archive.writestr(name, data)
            restored = DirectoryBlobs.prepare(tmp_path / "fresh-stage")
            with pytest.raises(LifecycleError):
                await bundle.restore(snapshot, target, restored)
            assert neighbor.read_bytes() == b"unrelated"
        finally:
            await w.close()

    asyncio.run(scenario())
