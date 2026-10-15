"""Replace the live database and index with the staged ones; keep what was replaced aside."""

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.local_files import restrict_private

from .directory import wipe

DATABASE = "database.sqlite"
INDEX = "index.sqlite"
SUFFIXES = ("", "-wal", "-shm")


class FileSwitcher:
    def __init__(self, live_database: Path, live_index: Path, aside: Path) -> None:
        self.live_database, self.live_index, self.aside = live_database, live_index, aside

    async def switch(self, staged_dir: Path) -> None:
        def work() -> None:
            self.aside.mkdir(parents=True, exist_ok=True, mode=0o700)
            restrict_private(self.aside, directory=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            kept = self.aside / stamp
            kept.mkdir(mode=0o700)
            restrict_private(kept, directory=True)
            moved: list[tuple[Path, Path]] = []
            try:
                for live in (self.live_database, self.live_index):
                    for suffix in SUFFIXES:
                        source = Path(str(live) + suffix)
                        if source.exists():
                            destination = kept / (source.name)
                            os.replace(source, destination)
                            moved.append((destination, source))
                for staged, live in (
                    (staged_dir / DATABASE, self.live_database),
                    (staged_dir / INDEX, self.live_index),
                ):
                    if staged.exists():
                        shutil.copyfile(staged, Path(str(live) + ".incoming"))
                        os.replace(Path(str(live) + ".incoming"), live)
                        restrict_private(live, directory=False)
            except BaseException:
                for destination, source in reversed(moved):  # put the old state back
                    os.replace(destination, source)
                raise

        await asyncio.to_thread(work)

    def replaced(self) -> list[str]:
        return sorted(p.name for p in self.aside.iterdir()) if self.aside.exists() else []

    def discard(self, name: str) -> None:
        """Operator action after checking the result: the replaced files are wiped."""
        directory = self.aside / name
        if directory.parent != self.aside or not directory.is_dir():
            raise ValueError(name)
        for item in directory.glob("*"):
            wipe(item)
        shutil.rmtree(directory, ignore_errors=True)
