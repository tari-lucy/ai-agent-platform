"""M5-03 fixtures: the M3-04 laboratory world, a recording index factory, a residue probe."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for helper in ("tests/laboratory", "tests/retention", "tests/memory", "tests/usage"):
    sys.path.insert(0, str(ROOT / helper))

from uuid import uuid4  # noqa: E402

from ai_agent_adapters.recall import SQLiteRecallIndex  # noqa: E402
from lab_support import new_env, run  # noqa: E402

__all__ = ["ROOT", "new_env", "recording_indexes", "residue", "run"]


def recording_indexes(env) -> list[Path]:
    """Swap the runner's index factory for one that remembers every index file it creates."""
    made: list[Path] = []

    def factory():
        path = env.base / f"{uuid4()}-index.sqlite"
        made.append(path)
        return SQLiteRecallIndex(path)

    env.runner.index_factory = factory
    return made


def residue(paths, needle: str) -> list[str]:
    """Files (database, WAL, shared memory, index) where the text is still physically present."""
    found, raw = [], needle.encode()
    for base in paths:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(base) + suffix)
            if path.exists() and raw in path.read_bytes():
                found.append(path.name)
    return found
