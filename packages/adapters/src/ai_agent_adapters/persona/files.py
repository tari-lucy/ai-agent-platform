"""Markdown profile files in one configured directory (outside Git, e.g. under ``data/``).

The operator's Markdown is an editable project, not an authority: reading yields text for the
persona service to validate; exporting writes a canonical rendering of an accepted snapshot.
Both stay inside the root, refuse links and non-regular files, and never overwrite silently.
"""

import os
import re
import stat
import sys
from pathlib import Path

from ai_agent_application.persona import (
    Issue,
    ProfileImportRejected,
)
from ai_agent_application.persona.schemas import MAX_MARKDOWN_BYTES
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.md$")


def _open_read_without_links(path: Path) -> int:
    if sys.platform == "win32":
        return _open_windows_without_reparse(path)
    # A preliminary lstat alone would leave a check/open race.
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("Safe file opening is unavailable")
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))


if sys.platform == "win32":

    def _open_windows_without_reparse(path: Path) -> int:
        # Import only on Windows; Linux has no msvcrt/WinDLL.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class AttributeTagInfo(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        query = kernel.GetFileInformationByHandleEx
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        query.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        # GENERIC_READ, shared read/write/delete, OPEN_EXISTING,
        # FILE_FLAG_OPEN_REPARSE_POINT: open the link itself, never its target.
        handle = create(str(path), 0x80000000, 0x7, None, 3, 0x00200000, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = AttributeTagInfo()
            # FileAttributeTagInfo=9; inspect the same open handle, not the path.
            if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                raise OSError("Reparse files are not accepted")
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
        # Ownership transfers to the fd; fdopen/close releases the Windows handle.
        return descriptor


class ProfileFiles:
    def __init__(self, root: Path) -> None:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self.root = resolved

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name) or ".." in name:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return self.root / name

    def read(self, name: str) -> str:
        """Return UTF-8 text; rejection carries a safe issue and never echoes file content."""
        path = self._path(name)
        try:
            descriptor = _open_read_without_links(path)
        except OSError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MARKDOWN_BYTES:
                raise ProfileImportRejected(
                    (Issue("FILE_REJECTED", "Файл не подходит для импорта."),)
                )
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            data = handle.read(MAX_MARKDOWN_BYTES + 1)
        if len(data) > MAX_MARKDOWN_BYTES:
            raise ProfileImportRejected((Issue("TOO_LARGE", "Документ превышает предел размера."),))
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ProfileImportRejected(
                (Issue("INVALID_ENCODING", "Файл должен быть в кодировке UTF-8."),)
            ) from None

    def export(self, name: str, markdown: str) -> None:
        """Create a new file atomically; an existing name is never replaced."""
        path = self._path(name)
        data = markdown.encode("utf-8")
        if len(data) > MAX_MARKDOWN_BYTES:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        temporary = self.root / (".export-" + os.urandom(8).hex() + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)  # fails if the name exists; no check-then-write race
        except OSError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        finally:
            temporary.unlink(missing_ok=True)
