"""Private flat backup extra for immutable blobs referenced by a SQLite snapshot.

No extraction of archive paths, no raw directory-tree copy, no secrets or runner
caches. Host backup/restore must stage this before ledger reapplication/switch.
"""

import asyncio
import contextlib
import hashlib
import os
import re
import sqlite3
import stat
import struct
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ai_agent_contracts.agent_workplace import ManagedFile
from ai_agent_contracts.common import Scope

from ai_agent_adapters.storage.policy import scope_key

from .directory import DirectoryBlobs, _anchor, _sync_directory
from .lifecycle import LifecycleError
from .safe_read import read_blob

BUNDLE_NAME = "workplace-blobs.zip"
NAME = re.compile(r"[0-9a-f]{64}/[0-9a-f]{64}")


@dataclass(frozen=True)
class BundleLimits:
    max_file_bytes: int
    max_bundle_bytes: int
    max_entries: int
    max_revisions: int
    max_metadata_bytes: int

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value < 1
                for value in (
                    self.max_file_bytes,
                    self.max_bundle_bytes,
                    self.max_entries,
                    self.max_revisions,
                    self.max_metadata_bytes,
                )
            )
            or self.max_entries > 60000
            or self.max_bundle_bytes >= 2**31
        ):
            raise LifecycleError("WORKPLACE_BUNDLE_LIMITS")


def _selection(database: Path, cap: BundleLimits) -> dict[str, tuple[Scope, str, int]]:
    if not database.is_absolute() or database.is_symlink():
        raise LifecycleError("WORKPLACE_SNAPSHOT_INVALID")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    entries: dict[str, tuple[Scope, str, int]] = {}
    count = size = 0
    try:
        for key, payload in connection.execute(
            "SELECT scope_id,payload FROM record_revisions WHERE kind='ManagedFile'"
        ):
            count += 1
            size += len(payload.encode())
            if count > cap.max_revisions or size > cap.max_metadata_bytes:
                raise LifecycleError("WORKPLACE_BUNDLE_LIMIT")
            record = ManagedFile.model_validate_json(payload)
            ref = record.reference
            if scope_key(record.scope) != key or ref.size_bytes > cap.max_file_bytes:
                raise LifecycleError("WORKPLACE_BUNDLE_REFERENCE")
            name = key + "/" + ref.checksum
            entry = record.scope, ref.checksum, ref.size_bytes
            if name in entries and entries[name] != entry:
                raise LifecycleError("WORKPLACE_BUNDLE_REFERENCE")
            entries[name] = entry
            if len(entries) > cap.max_entries:
                raise LifecycleError("WORKPLACE_BUNDLE_LIMIT")
    finally:
        connection.close()
    # Includes fixed local/central ZIP headers and leaves no unbounded ZIP64 space.
    if sum(item[2] + 512 for item in entries.values()) + 22 > cap.max_bundle_bytes:
        raise LifecycleError("WORKPLACE_BUNDLE_LIMIT")
    return entries


@contextmanager
def _read_bundle(path: Path, cap: BundleLimits) -> Iterator[zipfile.ZipFile]:
    if path.name != BUNDLE_NAME:
        raise LifecycleError("WORKPLACE_BUNDLE_NAME")
    with _anchor(path.parent) as parent:
        if sys.platform == "win32":
            from ._windows import open_read

            handle = open_read(path.parent, path.name)
        else:

            @contextmanager
            def opened() -> Iterator[int]:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    yield fd
                finally:
                    os.close(fd)

            handle = opened()
        with handle as fd:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 22 <= info.st_size <= cap.max_bundle_bytes
            ):
                raise LifecycleError("WORKPLACE_BUNDLE_LIMIT")
            with os.fdopen(os.dup(fd), "rb") as stream:
                # Our stored-only format has no ZIP64/comment/multiple volumes.
                # Bound the central directory BEFORE ZipFile allocates its entries.
                stream.seek(-22, os.SEEK_END)
                signature, disk, start, local_n, total_n, central_size, offset, comment = (
                    struct.unpack("<4s4H2LH", stream.read(22))
                )
                if (
                    signature != b"PK\x05\x06"
                    or disk
                    or start
                    or comment
                    or local_n != total_n
                    or total_n > cap.max_entries
                    or central_size > cap.max_entries * 512
                    or offset + central_size != info.st_size - 22
                ):
                    raise LifecycleError("WORKPLACE_BUNDLE_FORMAT")
                stream.seek(0)
                with zipfile.ZipFile(stream) as archive:
                    yield archive


class BlobBundle:
    def __init__(self, limits: BundleLimits) -> None:
        self.limits = limits

    def _create(self, database: Path, blobs: DirectoryBlobs, target: Path) -> None:
        if target.name != BUNDLE_NAME:
            raise LifecycleError("WORKPLACE_BUNDLE_NAME")
        entries = _selection(database, self.limits)
        with _anchor(target.parent) as parent:
            fd = os.open(
                target if parent is None else target.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "w+b") as stream:
                    with zipfile.ZipFile(
                        stream, "w", compression=zipfile.ZIP_STORED, allowZip64=False
                    ) as archive:
                        for name, (scope, digest, size) in sorted(entries.items()):
                            content = read_blob(
                                blobs._scope(scope), digest, max_bytes=self.limits.max_file_bytes
                            )
                            if len(content) != size:
                                raise LifecycleError("WORKPLACE_BUNDLE_REFERENCE")
                            entry = zipfile.ZipInfo(name)
                            entry.external_attr = 0o600 << 16
                            archive.writestr(entry, content)
                    stream.flush()
                    os.fsync(stream.fileno())
                    if stream.tell() > self.limits.max_bundle_bytes:
                        raise LifecycleError("WORKPLACE_BUNDLE_LIMIT")
                _sync_directory(parent)
            except BaseException:
                os.unlink(target if parent is None else target.name, dir_fd=parent)
                raise

    async def create(self, database: Path, blobs: DirectoryBlobs, target: Path) -> None:
        """From a consistent SQLite snapshot; missing blobs fail the whole backup."""
        worker = asyncio.create_task(asyncio.to_thread(self._create, database, blobs, target))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await worker
            raise
        except LifecycleError:
            raise
        except Exception:
            raise LifecycleError("WORKPLACE_BUNDLE_CREATE_FAILED") from None

    def _restore(self, database: Path, source: Path, target: DirectoryBlobs) -> None:
        entries = _selection(database, self.limits)
        with _anchor(target.root):
            if any(target.root.iterdir()):
                raise LifecycleError("WORKPLACE_RESTORE_NOT_EMPTY")
            with _read_bundle(source, self.limits) as archive:
                info = archive.infolist()
                names = [item.filename for item in info]
                if len(names) != len(set(names)) or set(names) != set(entries):
                    raise LifecycleError("WORKPLACE_BUNDLE_CONTENTS")
                for item in info:
                    if (
                        NAME.fullmatch(item.filename) is None
                        or item.compress_type != zipfile.ZIP_STORED
                        or item.flag_bits & 1
                        or item.compress_size != item.file_size
                        or item.file_size > self.limits.max_file_bytes
                        or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG)
                    ):
                        raise LifecycleError("WORKPLACE_BUNDLE_FORMAT")
                    scope, digest, size = entries[item.filename]
                    with archive.open(item) as stream:
                        content = stream.read(self.limits.max_file_bytes + 1)
                    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                        raise LifecycleError("WORKPLACE_BUNDLE_REFERENCE")
                    # Fresh offline staging root, no runtime/writer can see it yet.
                    target._put(scope, digest, content, self.limits.max_bundle_bytes)

    async def restore(self, database: Path, source: Path, target: DirectoryBlobs) -> None:
        """Only NEW private staging. Apply independent ledger BEFORE any host switch."""
        worker = asyncio.create_task(asyncio.to_thread(self._restore, database, source, target))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await worker
            raise
        except LifecycleError:
            raise
        except Exception:
            raise LifecycleError("WORKPLACE_BUNDLE_RESTORE_FAILED") from None
