"""The texts that shape the persona, editable from the panel (operator only).

Only a fixed whitelist can be touched: a few files at the top of her workspace, the markdown
and text files of ``playbook/`` and ``servers/``, and the operator rules file (guide/AGENTS.md).
Nothing else on the disk is reachable: an id is matched against the whitelist, never turned
into an arbitrary path. A save needs the version the operator edited from (a SHA-256 of the
content), because she edits the same files herself: a changed version is a conflict, not an
overwrite. The replaced text is kept (the last 30 versions) and every save is journaled.
"""

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import (
    PersonaText,
    PersonaTextEntry,
    PersonaTextRevision,
    PersonaTextRevisionBody,
    PersonaTexts,
)
from ai_agent_contracts.states import ErrorCode

MAX_BYTES = 65_536
KEEP = 30
TOP_FILES = {
    "README.md": ("plans", "Описание рабочего места (README)"),
    "GOALS.md": ("plans", "Цели и интересы (GOALS)"),
    "editor.md": ("plans", "Редакторский вкус и границы"),
    "activities.md": ("plans", "Её список занятий"),
    "ideas.md": ("plans", "Её идеи"),
}
DIRS = {"playbook": ("guides", ".md", ".txt"), "servers": ("servers", ".md")}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.(md|txt)$")
GUIDE_ID = "guide:AGENTS.md"
EFFECT_FILE = "Агент прочитает изменение, когда в следующий раз откроет этот файл (обычно в начале занятия)."
EFFECT_GUIDE = "Правила читаются при запуске: изменение подействует при следующем START."


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Resolved:
    def __init__(self, text_id: str, group: str, title: str, path: Path) -> None:
        self.id, self.group, self.title, self.path = text_id, group, title, path


class TextStore:
    """Reads and writes the whitelisted texts of the active persona."""

    def __init__(self, harness: Any) -> None:
        self.harness = harness

    # --- whitelist -------------------------------------------------------------------------
    def _folder(self) -> Path | None:
        return None if self.harness is None else self.harness.folders()

    def _guide(self) -> Path | None:
        return getattr(self.harness.settings, "guide_file", None) if self.harness else None

    def _history(self) -> Path | None:
        return getattr(self.harness.settings, "history_dir", None) if self.harness else None

    def resolve(self, text_id: str) -> Resolved:
        """The path of a whitelisted id; anything else is an invalid argument."""
        if text_id == GUIDE_ID:
            guide = self._guide()
            if guide is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            return Resolved(text_id, "rules", "Правила оператора (AGENTS.md)", guide)
        folder = self._folder()
        if folder is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        parts = text_id.split(":")
        if parts[0] != "file" or not 2 <= len(parts) <= 3 or not NAME.fullmatch(parts[-1]):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if len(parts) == 2:
            if parts[1] not in TOP_FILES:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            group, title = TOP_FILES[parts[1]]
            return Resolved(text_id, group, title, folder / parts[1])
        rule = DIRS.get(parts[1])
        if rule is None or not parts[2].endswith(rule[1:]):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return Resolved(text_id, rule[0], f"{parts[1]}/{parts[2]}", folder / parts[1] / parts[2])

    def _safe_file(self, path: Path) -> bool:
        return path.is_file() and not path.is_symlink() and not path.parent.is_symlink()

    # --- reading ---------------------------------------------------------------------------
    def listing(self, scope: Scope, now: datetime) -> PersonaTexts:
        stamp = now.isoformat(timespec="seconds")
        folder = self._folder()
        if self.harness is None or folder is None:
            return PersonaTexts(scope=scope, enabled=False, checked_at=stamp)
        found: list[Resolved] = []
        guide = self._guide()
        if guide is not None:
            found.append(Resolved(GUIDE_ID, "rules", "Правила оператора (AGENTS.md)", guide))
        for name, (group, title) in TOP_FILES.items():
            found.append(Resolved(f"file:{name}", group, title, folder / name))
        for directory, (group, *suffixes) in DIRS.items():
            root = folder / directory
            try:
                if root.is_symlink() or not root.is_dir():
                    continue
                names = sorted(p.name for p in root.iterdir() if p.suffix in suffixes)
            except OSError:
                continue
            for name in names:
                if NAME.fullmatch(name):
                    found.append(Resolved(f"file:{directory}:{name}", group, f"{directory}/{name}", root / name))
        items: list[PersonaTextEntry] = []
        for entry in found[:80]:
            if not self._safe_file(entry.path):
                if entry.id == GUIDE_ID or entry.path.parent == folder:
                    items.append(PersonaTextEntry(  # type: ignore[arg-type]
                        id=entry.id, group=entry.group, title=entry.title, size=0, updated_at="", writable=True))
                continue
            try:
                stat = entry.path.stat()
            except OSError:
                continue
            items.append(PersonaTextEntry(  # type: ignore[arg-type]
                id=entry.id, group=entry.group, title=entry.title, size=stat.st_size,
                updated_at=_iso(stat.st_mtime), writable=stat.st_size <= MAX_BYTES))
        return PersonaTexts(scope=scope, enabled=True, items=items, checked_at=stamp)

    def _revisions(self, text_id: str) -> list[PersonaTextRevision]:
        root = self._revision_dir(text_id)
        rows: list[PersonaTextRevision] = []
        try:
            if root is None or not root.is_dir():
                return []
            for path in sorted(root.glob("*.txt"), reverse=True)[:KEEP]:
                rows.append(PersonaTextRevision(
                    rev=f"{text_id}@{path.stem}", at=_iso(path.stat().st_mtime), size=path.stat().st_size))
        except OSError:
            return []
        return rows

    def _revision_dir(self, text_id: str) -> Path | None:
        history = self._history()
        if history is None:
            return None
        persona = self.harness.persona()
        return history / re.sub(r"[^A-Za-z0-9._-]", "_", persona) / re.sub(r"[^A-Za-z0-9._-]", "_", text_id)

    def read(self, scope: Scope, text_id: str) -> PersonaText:
        entry = self.resolve(text_id)
        content, version, updated = "", "new", ""
        if self._safe_file(entry.path):
            try:
                data = entry.path.read_bytes()
                stat = entry.path.stat()
            except OSError:
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
            if len(data) > MAX_BYTES:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            content, version, updated = data.decode("utf-8", errors="replace"), _sha(data), _iso(stat.st_mtime)
        elif entry.path.exists():
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return PersonaText(
            scope=scope, id=text_id, title=entry.title, content=content, version=version,  # type: ignore[arg-type]
            updated_at=updated, writable=True,
            effect=EFFECT_GUIDE if text_id == GUIDE_ID else EFFECT_FILE,
            history=self._revisions(text_id),
        )

    def revision(self, scope: Scope, rev: str) -> PersonaTextRevisionBody:
        text_id, _, stamp = rev.partition("@")
        self.resolve(text_id)  # the id must still be whitelisted
        root = self._revision_dir(text_id)
        if root is None or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z(-[0-9a-f]{6})?", stamp):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        path = root / f"{stamp}.txt"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            return PersonaTextRevisionBody(
                scope=scope, rev=rev, id=text_id, at=_iso(path.stat().st_mtime),  # type: ignore[arg-type]
                content=path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None

    # --- writing ---------------------------------------------------------------------------
    def write(self, scope: Scope, principal: str, text_id: str, content: str, base: str) -> PersonaText:
        """Replace one text. Conflict if it changed since the operator opened it."""
        if scope.memory_mode != "work" or scope.persona_id != self.harness.persona():
            raise ContractError(ErrorCode.ACCESS_DENIED)  # only the active persona's real texts
        entry = self.resolve(text_id)
        data = content.encode("utf-8")
        if len(data) > MAX_BYTES or b"\x00" in data:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        existing: bytes | None = None
        if entry.path.is_symlink() or (entry.path.exists() and not entry.path.is_file()):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if entry.path.exists():
            try:
                existing = entry.path.read_bytes()
            except OSError:
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
        if base != ("new" if existing is None else _sha(existing)):
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            if existing is not None:
                self._keep(text_id, stamp, existing)
            entry.path.parent.mkdir(parents=True, exist_ok=True)
            part = entry.path.with_name(f".part-{uuid.uuid4().hex}")
            part.write_bytes(data)
            os.chmod(part, 0o664)  # the persona's container user edits these files too
            os.replace(part, entry.path)
            self._journal(principal, text_id, existing, data, stamp)
        except OSError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
        return self.read(scope, text_id)

    def _keep(self, text_id: str, stamp: str, old: bytes) -> None:
        root = self._revision_dir(text_id)
        if root is None:
            return
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{stamp}.txt"
        if target.exists():
            target = root / f"{stamp}-{uuid.uuid4().hex[:6]}.txt"
        target.write_bytes(old)
        for stale in sorted(root.glob("*.txt"), reverse=True)[KEEP:]:
            stale.unlink(missing_ok=True)

    def _journal(self, principal: str, text_id: str, before: bytes | None, after: bytes, stamp: str) -> None:
        history = self._history()
        if history is None:
            return
        history.mkdir(parents=True, exist_ok=True)
        line = {
            "at": stamp, "operator": principal[:64], "id": text_id,
            "before": None if before is None else _sha(before)[:16], "after": _sha(after)[:16], "size": len(after),
        }
        with (history / "journal.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")
