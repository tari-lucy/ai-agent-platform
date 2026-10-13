"""Markdown profile files in one configured directory (outside Git, e.g. under ``data/``).

The operator's Markdown is an editable project, not an authority: reading yields text for the
persona service to validate; exporting writes a canonical rendering of an accepted snapshot.
Both stay inside the root, refuse links and non-regular files, and never overwrite silently.
"""

import os
import re
import stat
from pathlib import Path

from ai_agent_application.persona import (
    Issue,
    ProfileImportRejected,
)
from ai_agent_application.persona.schemas import MAX_MARKDOWN_BYTES
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.md$")


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
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
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
