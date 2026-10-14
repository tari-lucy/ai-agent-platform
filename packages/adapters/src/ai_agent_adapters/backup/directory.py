"""Directory store of backups: each copy appears whole (temp dir + rename) or not at all."""

import json
import os
import shutil
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ai_agent_application.backup import FileEntry, Manifest
from ai_agent_application.backup.model import Kind

MANIFEST = "manifest.json"
WIPE_CHUNK = 1 << 20


def wipe(path: Path) -> None:
    """Best effort: overwrite then unlink. Not a guarantee on SSD / copy-on-write volumes."""
    if not path.is_file():
        return
    size = path.stat().st_size
    with path.open("r+b") as handle:
        for _ in range(0, size, WIPE_CHUNK):
            handle.write(b"\0" * min(WIPE_CHUNK, size))
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()


def _encode(manifest: Manifest) -> str:
    return json.dumps(
        {
            "format": manifest.format,
            "backup_id": manifest.backup_id,
            "kind": manifest.kind,
            "created_at": manifest.created_at.isoformat(),
            "schema_head": manifest.schema_head,
            "ledger_id": manifest.ledger_id,
            "ledger_seq": manifest.ledger_seq,
            "software": manifest.software,
            "files": [{"name": f.name, "sha256": f.sha256, "size": f.size} for f in manifest.files],
        },
        indent=2,
    )


def _decode(text: str) -> Manifest:
    raw = json.loads(text)
    return Manifest(
        raw["format"],
        raw["backup_id"],
        raw["kind"],
        datetime.fromisoformat(raw["created_at"]),
        raw["schema_head"],
        raw["ledger_id"],
        int(raw["ledger_seq"]),
        tuple(FileEntry(f["name"], f["sha256"], int(f["size"])) for f in raw["files"]),
        raw.get("software", ""),
    )


class DirectoryBackupStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def used_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    async def write(
        self, backup_id: str, kind: Kind, files: Sequence[tuple[str, Path]], manifest: Manifest
    ) -> None:
        final = self.root / backup_id
        if final.exists():
            raise FileExistsError(backup_id)
        temporary = self.root / f".{backup_id}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir(mode=0o700)
        try:
            for name, source in files:
                target = temporary / name
                shutil.copyfile(source, target)
                target.chmod(0o600)
            (temporary / MANIFEST).write_text(_encode(manifest), encoding="utf-8")
            (temporary / MANIFEST).chmod(0o600)
            for item in temporary.iterdir():  # durable before the copy becomes visible
                with item.open("r+b") as handle:
                    os.fsync(handle.fileno())
            temporary.rename(final)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    async def manifests(self) -> list[Manifest]:
        found = []
        for entry in sorted(self.root.iterdir()):
            file = entry / MANIFEST
            if entry.is_dir() and not entry.name.startswith(".") and file.is_file():
                found.append(_decode(file.read_text(encoding="utf-8")))
        return found

    async def path(self, backup_id: str, name: str) -> Path:
        if "/" in name or "\\" in name or ".." in backup_id or "/" in backup_id:
            raise ValueError(name)
        return self.root / backup_id / name

    async def remove(self, backup_id: str) -> None:
        directory = self.root / backup_id
        for item in directory.glob("*"):
            wipe(item)
        shutil.rmtree(directory, ignore_errors=True)
