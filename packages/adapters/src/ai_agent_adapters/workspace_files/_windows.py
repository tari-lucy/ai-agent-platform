"""Pin every directory, then open the final file without following reparse points.

Directory handles omit FILE_SHARE_DELETE so a path cannot be redirected by rename
while the read is in progress. This is deliberately separate from Path.resolve().
"""

import ctypes
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _Kernel:
    # ctypes bindings exist only on Windows. Explicit dynamic types also let
    # Linux mypy inspect this module without inventing a callable signature.
    create: Any
    query: Any
    kind: Any
    close: Any

    def __init__(self) -> None:
        # Evaluated only on Windows, never while importing the adapter on Linux.
        if sys.platform != "win32":
            raise OSError("BLOB_SAFE_IO_UNAVAILABLE")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.create = kernel.CreateFileW
        self.create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.create.restype = wintypes.HANDLE
        self.query = kernel.GetFileInformationByHandleEx
        self.query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        self.query.restype = wintypes.BOOL
        self.kind = kernel.GetFileType
        self.kind.argtypes = [wintypes.HANDLE]
        self.kind.restype = wintypes.DWORD
        self.close = kernel.CloseHandle
        self.close.argtypes = [wintypes.HANDLE]
        self.close.restype = wintypes.BOOL

    def open(self, path: Path, *, directory: bool) -> int:
        # FILE_LIST_DIRECTORY (data access) for directories, GENERIC_READ for files.
        # Attribute-only opens do NOT participate in Win32 sharing checks.
        # Directory: share read/write, but no delete/rename. File: share only read.
        handle = self.create(
            str(path),
            0x1 if directory else 0x80000000,
            0x3 if directory else 0x1,
            None,
            3,  # OPEN_EXISTING; never create or truncate
            0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
            None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise OSError("BLOB_UNAVAILABLE")
        try:
            info = _AttributeTagInfo()
            if not self.query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError("BLOB_UNAVAILABLE")
            if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                raise OSError("BLOB_UNSAFE_PATH")
            if bool(info.attributes & 0x10) != directory or self.kind(handle) != 1:
                raise OSError("BLOB_NOT_REGULAR")
            return int(handle)
        except BaseException:
            self.close(handle)
            raise


@contextmanager
def pinned_root(root: Path) -> Iterator[_Kernel]:
    if sys.platform != "win32":
        raise OSError("BLOB_SAFE_IO_UNAVAILABLE")
    kernel = _Kernel()
    pins: list[int] = []
    try:
        # Ancestors are pinned in order; each next component is inspected through
        # its own handle before using it as the parent of another open operation.
        for directory in reversed((root, *root.parents)):
            pins.append(kernel.open(directory, directory=True))
        yield kernel
    finally:
        for handle in reversed(pins):
            kernel.close(handle)


@contextmanager
def open_read(root: Path, name: str) -> Iterator[int]:
    if sys.platform != "win32":
        raise OSError("BLOB_SAFE_IO_UNAVAILABLE")
    import msvcrt

    with pinned_root(root) as kernel:
        handle = kernel.open(root / name, directory=False)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.close(handle)
            raise
        try:
            yield descriptor
        finally:
            os.close(descriptor)
