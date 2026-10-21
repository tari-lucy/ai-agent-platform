"""Private immutable blobs. Catalogue transactions serialize quota decisions and writers.

No model-supplied paths, no credentials, no second metadata registry. Orphans count
toward quota and are never exposed without a committed ManagedFile reference.
"""

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_agent_contracts.common import Scope

from ai_agent_adapters.local_files import restrict_private

from .safe_read import UnsafeBlobError, read_blob


@contextmanager
def _anchor(root: Path) -> Iterator[int | None]:
    if not root.is_absolute() or ".." in root.parts:
        raise UnsafeBlobError("BLOB_ROOT_INVALID")
    if sys.platform == "win32":
        from ._windows import pinned_root

        if re.fullmatch(r"[A-Za-z]:", root.drive) is None or any(
            part.endswith((".", " ")) or ":" in part for part in root.parts[1:]
        ):
            raise UnsafeBlobError("BLOB_ROOT_INVALID")
        with pinned_root(root):
            yield None
    else:
        if root.anchor != "/" or not hasattr(os, "O_NOFOLLOW"):
            raise UnsafeBlobError("BLOB_SAFE_IO_UNAVAILABLE")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(root.anchor, flags)
        try:
            for part in root.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)


def _sync_directory(descriptor: int | None) -> None:
    if descriptor is not None:
        os.fsync(descriptor)


class DirectoryBlobs:
    """Root is prepared explicitly by trusted setup, never by agent arguments.

    Operations must run under the catalogue owner's serialized transaction. The
    root is private and must never be mounted into a runner or exposed as a path API.
    """

    def __init__(self, root: Path) -> None:
        with _anchor(root):
            pass
        self.root = root

    @staticmethod
    def prepare(root: Path) -> DirectoryBlobs:
        """Explicit setup of a NEW empty store, with owner-only inherited permissions."""
        with _anchor(root.parent) as parent:
            if parent is None:
                root.mkdir(mode=0o700)
            else:
                os.mkdir(root.name, mode=0o700, dir_fd=parent)
                _sync_directory(parent)
            with _anchor(root):
                restrict_private(root, directory=True)
        return DirectoryBlobs(root)

    def _scope(self, scope: Scope, *, create: bool = False) -> Path:
        name = hashlib.sha256(
            json.dumps(scope.partition, separators=(",", ":")).encode()
        ).hexdigest()
        with _anchor(self.root) as parent:
            if create:
                try:
                    if parent is None:
                        (self.root / name).mkdir(mode=0o700)
                    else:
                        os.mkdir(name, mode=0o700, dir_fd=parent)
                    _sync_directory(parent)
                except FileExistsError:
                    pass
            with _anchor(self.root / name):
                pass  # reject substituted/reparse directories before any content IO
        return self.root / name

    def _used(self, root: Path) -> int:
        with _anchor(root) as descriptor:
            return self._scan_usage(root, descriptor)

    def _scan_usage(self, root: Path, descriptor: int | None) -> int:
        total = 0
        with os.scandir(root if descriptor is None else descriptor) as entries:
            for entry in entries:
                # Windows DirEntry.stat caches find-data with st_nlink=0.
                # Query the entry itself while its parent remains pinned.
                info = os.stat(
                    root / entry.name if descriptor is None else entry.name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise UnsafeBlobError("BLOB_STORAGE_UNSAFE")
                # Include every byte, even orphaned staging files after a crash.
                total += info.st_size
        return total

    def _put(self, scope: Scope, digest: str, data: bytes, max_scope_bytes: int) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise UnsafeBlobError("BLOB_INTEGRITY_FAILED")
        root = self._scope(scope, create=True)
        with _anchor(root) as parent:
            try:
                existing = read_blob(root, digest, max_bytes=len(data))
            except UnsafeBlobError as error:
                # Only a truly absent leaf permits creation; corruption is never repaired blindly.
                try:
                    os.stat(
                        root / digest if parent is None else digest,
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise error
            else:
                if existing != data:
                    raise UnsafeBlobError("BLOB_INTEGRITY_FAILED")
                return
            if self._scan_usage(root, parent) + len(data) > max_scope_bytes:
                raise UnsafeBlobError("BLOB_QUOTA")
            temporary = "pending-" + uuid4().hex
            path = root / temporary if parent is None else temporary
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if parent is None:
                    os.rename(root / temporary, root / digest)  # Windows refuses an existing leaf
                else:
                    os.link(
                        temporary,
                        digest,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                    os.unlink(temporary, dir_fd=parent)
                _sync_directory(parent)
            finally:
                try:
                    os.unlink(path, dir_fd=parent)
                except FileNotFoundError:
                    pass

    async def put(self, scope: Scope, digest: str, data: bytes, max_scope_bytes: int) -> None:
        await asyncio.to_thread(self._put, scope, digest, data, max_scope_bytes)

    async def read(self, scope: Scope, digest: str, max_bytes: int) -> bytes:
        return await asyncio.to_thread(read_blob, self._scope(scope), digest, max_bytes=max_bytes)

    async def used_bytes(self, scope: Scope) -> int:
        return await asyncio.to_thread(self._used, self._scope(scope))
