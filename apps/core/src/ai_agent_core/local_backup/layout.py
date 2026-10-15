"""Explicit, non-overlapping locations of copies and of the independent ledger."""

from dataclasses import dataclass
from pathlib import Path

from ai_agent_core.local_host.layout import HostError, HostPaths, paths

# The only files a copy may carry besides the database. `.env`, keys, operator password/hash,
# logs and personal external directories are not on this list and are never added.
HOST_CONFIG = "host.json"
ALLOWED_EXTRA = frozenset({HOST_CONFIG})


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


@dataclass(frozen=True)
class BackupLayout:
    host: HostPaths
    store_dir: Path
    ledger_path: Path

    @property
    def work_dir(self) -> Path:
        return self.store_dir / ".work"

    @property
    def replaced_dir(self) -> Path:
        return self.store_dir / ".replaced"

    @property
    def ledger_copies(self) -> Path:
        return self.ledger_path.with_name(self.ledger_path.name + ".copies")


def layout(root: Path, store_dir: Path, ledger_path: Path) -> BackupLayout:
    """Absolute paths only; copies and ledger never inside or around the live host."""
    host = paths(root)
    for item in (store_dir, ledger_path):
        if not item.is_absolute():
            raise HostError("ABSOLUTE_PATH_REQUIRED")
        if item.is_symlink():
            raise HostError("BACKUP_PATH_UNSAFE")
    result = BackupLayout(host, store_dir, ledger_path)
    places = {
        "host": host.root.resolve(),
        "store": store_dir.resolve(),
        "ledger": ledger_path.resolve(),
        "ledger_copies": result.ledger_copies.resolve(),
    }
    names = tuple(places)
    for number, first in enumerate(names):
        for second in names[number + 1 :]:
            if _overlap(places[first], places[second]):
                raise HostError("BACKUP_PATHS_OVERLAP")
    return result
