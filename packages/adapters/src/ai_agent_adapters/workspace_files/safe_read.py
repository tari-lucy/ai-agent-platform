"""Bounded immutable-blob reads, not a public file API or a permission decision.

The caller is a trusted owner supplying an already authorised private root,
expected content hash and configured byte limit. The agent never mounts this
store. No metadata, grants, owner registry or source of truth is introduced here.
"""

import hashlib
import hmac
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class UnsafeBlobError(OSError):
    """A fixed safe code, without file paths or file contents."""


@contextmanager
def _posix_read(root: Path, name: str) -> Iterator[int]:
    if sys.platform == "win32":
        raise UnsafeBlobError("BLOB_SAFE_IO_UNAVAILABLE")
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise UnsafeBlobError("BLOB_SAFE_IO_UNAVAILABLE")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(root.anchor, directory_flags)
    descriptor: int | None = None
    try:
        for component in root.parts[1:]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        # O_NONBLOCK ensures a substituted FIFO cannot hang before fstat rejects it.
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
        )
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _regular(descriptor: int, limit: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeBlobError("BLOB_NOT_REGULAR")
    if info.st_size > limit:
        raise UnsafeBlobError("BLOB_TOO_LARGE")


def read_blob(root: Path, digest: str, *, max_bytes: int) -> bytes:
    """Read one SHA-256-named file; reject links, redirection, growth and corruption.

    No pathname from a model is accepted. Scope, TTL, current rights and deletion
    checks are the owner's responsibility before AND after this filesystem call.
    """
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise UnsafeBlobError("BLOB_ID_INVALID")
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or ".." in root.parts
        or (sys.platform != "win32" and root.anchor != "/")
    ):
        raise UnsafeBlobError("BLOB_ROOT_INVALID")
    if sys.platform == "win32" and (
        re.fullmatch(r"[A-Za-z]:", root.drive) is None
        or any(part.endswith((".", " ")) or ":" in part for part in root.parts[1:])
    ):
        # No UNC/device paths, alternate streams or Win32 ambiguous path aliases.
        raise UnsafeBlobError("BLOB_ROOT_INVALID")
    if type(max_bytes) is not int or max_bytes < 0:
        raise UnsafeBlobError("BLOB_LIMIT_INVALID")
    try:
        if sys.platform == "win32":
            from ._windows import open_read
        else:
            open_read = _posix_read
        with open_read(root, digest) as descriptor:
            _regular(descriptor, max_bytes)
            data = bytearray()
            while len(data) <= max_bytes:
                chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            _regular(descriptor, max_bytes)
            if len(data) > max_bytes:
                raise UnsafeBlobError("BLOB_TOO_LARGE")
            if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
                raise UnsafeBlobError("BLOB_INTEGRITY_FAILED")
            return bytes(data)
    except UnsafeBlobError:
        raise
    except OSError:
        raise UnsafeBlobError("BLOB_UNAVAILABLE") from None
