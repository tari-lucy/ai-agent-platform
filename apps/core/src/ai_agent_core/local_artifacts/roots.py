"""Verified absolute roots of derived files. Nothing outside them is ever listed or touched."""

import os
from dataclasses import dataclass
from pathlib import Path

INDEX_SUFFIX = ".sqlite"
REPORT_SUFFIXES = frozenset({".json", ".ttl", ".pending"})
COMPANIONS = ("-wal", "-shm")


class ArtifactsError(Exception):
    """Fixed code only; never a path, a memory text or a chained exception message."""

    def __init__(self, code: str, reasons: tuple[str, ...] = ()) -> None:
        self.code, self.reasons = code, reasons  # reasons: fixed codes of every failed step
        super().__init__(code)


def _link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


@dataclass(frozen=True)
class DerivedRoots:
    indexes: Path  # one SQLite recall index file per lab run
    reports: Path  # report bodies and their expiry markers


def verified_roots(indexes: Path, reports: Path) -> DerivedRoots:
    """Absolute existing directories, no links, not equal to or inside each other."""
    for root in (indexes, reports):
        if not root.is_absolute():
            raise ArtifactsError("ABSOLUTE_PATH_REQUIRED")
        if _link(root) or not root.is_dir():
            raise ArtifactsError("DERIVED_ROOT_UNSAFE")
    first, second = indexes.resolve(), reports.resolve()
    if first == second or first in second.parents or second in first.parents:
        raise ArtifactsError("DERIVED_ROOTS_OVERLAP")
    return DerivedRoots(first, second)


def regular_files(root: Path, suffixes: frozenset[str] | str) -> list[Path]:
    """Plain files directly inside the root. A link anywhere inside closes the operation."""
    wanted = {suffixes} if isinstance(suffixes, str) else suffixes
    found: list[Path] = []
    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            path = Path(entry.path)
            if _link(path):
                raise ArtifactsError("DERIVED_PATH_UNSAFE")
            if path.suffix in wanted and entry.is_file(follow_symlinks=False):
                found.append(path)
    return found


def companions(index: Path) -> list[Path]:
    """The -wal/-shm files that belong to one index file (links among them are refused)."""
    result = []
    for suffix in COMPANIONS:
        extra = Path(str(index) + suffix)
        if _link(extra):
            raise ArtifactsError("DERIVED_PATH_UNSAFE")
        if extra.exists():
            result.append(extra)
    return result
