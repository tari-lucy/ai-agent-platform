"""What the persona does right now, for the panel's home screen (read-only).

Everything here is read from the harness's own state and its local journals; nothing is
guessed. Each source is optional: a missing file or a part that is not wired gives fewer
lines, never an error and never an invented line. The text is her own public speech, what
she heard, and short event lines; the operator reads the same journals back anyway.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import HarnessNow, NowLine

MAX_LINES = 40
PER_SOURCE = 8
GOALS_CHARS = 1500
SOURCES = {
    "game": "игра", "channel": "канал", "operator": "вы", "gm": "гейммастер", "time": "время",
    "idle": "простой", "server": "сервер", "mail": "почта",
}
TAIL_BYTES = 32_768  # a journal can be long; the last lines are all that is read


def _tail(path: Path | None, count: int) -> list[dict[str, Any]]:
    if path is None or path.is_symlink():
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            raw = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines()[-count - 1 :]:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # the first line of the tail may be cut in half
        if isinstance(row, dict):
            rows.append(row)
    return rows[-count:]


def _clean(text: Any, limit: int = 280) -> str:
    return " ".join(str(text or "").split())[:limit]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _goals(folder: Path | None) -> str | None:
    if folder is None:
        return None
    path = folder / "GOALS.md"
    try:
        if path.is_symlink():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")[:GOALS_CHARS].strip()
    except OSError:
        return None
    return text or None


def build(harness: Any, scope: Scope, now: datetime) -> HarnessNow:
    """The picture of one moment; ``harness`` is the HarnessHost or None."""
    stamp = now.isoformat(timespec="seconds")
    if harness is None:
        return HarnessNow(scope=scope, enabled=False, checked_at=stamp)
    from .harness_activity import Activity
    from .harness_watch import Watch

    doing: str | None = None
    recently: list[str] = []
    lines: list[NowLine] = []
    for part in harness.parts:
        if isinstance(part, Activity):
            if part.running:
                what, _ = max(part.running.values(), key=lambda item: item[1])
                doing = _clean(what)
            recently = [_clean(item, 200) for item in list(part.recent)[-PER_SOURCE:]]
        elif isinstance(part, Watch):
            events = sorted(part.queue.events.values(), key=lambda e: e.created, reverse=True)
            for event in events[:PER_SOURCE]:
                label = f"{SOURCES.get(event.source, event.source)}: {_clean(event.text)}"
                lines.append(NowLine(at=_iso(event.created), kind="event", text=label[:300]))
    speaker = getattr(harness, "speaker", None)
    for row in _tail(getattr(speaker, "journal", None), PER_SOURCE):
        if row.get("status") == "spoken" and row.get("text"):
            lines.append(NowLine(at=str(row.get("at", ""))[:40], kind="said", text=_clean(row["text"])))
    listener = getattr(harness, "listener", None)
    for row in _tail(getattr(listener, "journal", None), PER_SOURCE):
        if row.get("text"):
            lines.append(NowLine(at=str(row.get("at", ""))[:40], kind="heard", text=_clean(row["text"])))
    lines.sort(key=lambda line: line.at, reverse=True)
    return HarnessNow(
        scope=scope,
        enabled=True,
        doing=doing,
        recently=recently,
        goals=_goals(harness.folders()),
        lines=lines[:MAX_LINES],
        checked_at=stamp,
    )
