"""What the persona does right now, for the panel's home screen (read-only).

Everything here is read from the harness's own state and its local journals; nothing is
guessed. Each source is optional: a missing file or a part that is not wired gives fewer
lines, never an error and never an invented line. The text is her own public speech, what
she heard, and short event lines; the operator reads the same journals back anyway.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import (
    HarnessNow,
    HarnessPicture,
    HourBucket,
    NowActivity,
    NowLine,
    NowPicture,
)

MAX_LINES = 40
PER_SOURCE = 8
GOALS_CHARS = 1500
SOURCES = {
    "game": "игра", "channel": "канал", "operator": "вы", "gm": "гейммастер", "time": "время",
    "idle": "простой", "server": "сервер", "mail": "почта",
}
TAIL_BYTES = 32_768  # a journal can be long; the last lines are all that is read
HOURS_BYTES = 393_216  # a day of journal lines for the hourly ribbon
PICTURES = 6
PICTURE_BYTES = 1_000_000  # base64 of this fits the contract's limit
PICTURE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(jpg|jpeg|png|webp)$")
MEDIA = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
STATES = {"идёт": "doing", "идет": "doing", "ждёт": "waiting", "ждет": "waiting",
          "план": "planned", "готово": "done"}
HEADING = re.compile(r"^##\s+(.+?)(?:\s*\[([^\]]+)\])?\s*$")
ORDER = {"doing": 0, "waiting": 1, "planned": 2, "done": 3}


def _tail(path: Path | None, count: int, size_limit: int = TAIL_BYTES) -> list[dict[str, Any]]:
    if path is None or path.is_symlink():
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - size_limit))
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


def _activities(folder: Path | None) -> list[NowActivity]:
    """Her own list of what she is up to: ``## Title [state]`` and the details under it."""
    if folder is None:
        return []
    path = folder / "activities.md"
    try:
        if path.is_symlink():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")[:20_000]
    except OSError:
        return []
    found: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            found.append((match.group(1).strip(), STATES.get((match.group(2) or "").strip().lower(), "planned"), []))
        elif found:
            found[-1][2].append(line)
    rows = [
        NowActivity(title=title[:80], state=state, detail="\n".join(body).strip()[:1200])  # type: ignore[arg-type]
        for title, state, body in found
        if title
    ]
    rows.sort(key=lambda row: ORDER[row.state])
    done = [row for row in rows if row.state == "done"][:3]
    return ([row for row in rows if row.state != "done"] + done)[:12]


def _hour(at: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _hours(harness: Any, now: datetime) -> list[HourBucket]:
    """Counts per hour for the last day from her own journals: spoken, heard, posted, drawn."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    slots = [{"said": 0, "heard": 0, "posted": 0, "drew": 0} for _ in range(24)]
    speaker = getattr(harness, "speaker", None)
    listener = getattr(harness, "listener", None)
    base = getattr(listener, "journal", None) or getattr(speaker, "journal", None)
    folder = None if base is None else base.parent
    sources = (
        ("said", getattr(speaker, "journal", None), lambda row: row.get("status") == "spoken"),
        ("heard", getattr(listener, "journal", None), lambda row: bool(row.get("text"))),
        ("posted", None if folder is None else folder / "channel.jsonl",
         lambda row: row.get("outcome") == "posted"),
        ("drew", None if folder is None else folder / "imagine.jsonl",
         lambda row: row.get("status") == "made"),
    )
    for key, path, wanted in sources:
        for row in _tail(path, 5000, HOURS_BYTES):
            moment = _hour(row.get("at"))
            if moment is None or not wanted(row):
                continue
            index = int((moment - start).total_seconds() // 3600)
            if 0 <= index < 24:
                slots[index][key] += 1
    return [
        HourBucket(hour=(start + timedelta(hours=i)).isoformat(timespec="seconds"), **slot)
        for i, slot in enumerate(slots)
    ]


def _imagine_log(harness: Any) -> Path | None:
    listener = getattr(harness, "listener", None)
    speaker = getattr(harness, "speaker", None)
    base = getattr(listener, "journal", None) or getattr(speaker, "journal", None)
    return None if base is None else base.parent / "imagine.jsonl"


def _pictures(harness: Any, folder: Path | None) -> list[NowPicture]:
    """Her latest drawings that still lie in her art folder, newest first."""
    if folder is None:
        return []
    found: list[NowPicture] = []
    for row in reversed(_tail(_imagine_log(harness), 40, HOURS_BYTES)):
        name = str(row.get("file", ""))
        if row.get("status") != "made" or not PICTURE_NAME.fullmatch(name):
            continue
        path = folder / "art" / name
        if path.is_symlink() or not path.is_file() or any(p.name == name for p in found):
            continue
        caption = _clean(row.get("scene") or row.get("meaning") or "", 200)
        found.append(NowPicture(name=name, at=str(row.get("at", ""))[:40], caption=caption))
        if len(found) == PICTURES:
            break
    return found


def picture(harness: Any, scope: Scope, name: str) -> HarnessPicture | None:
    """One drawing by name; only a name that the log of her drawings lists is served."""
    if harness is None or not PICTURE_NAME.fullmatch(name):
        return None
    folder = harness.folders()
    if folder is None or name not in {item.name for item in _pictures(harness, folder)}:
        return None
    path = folder / "art" / name
    try:
        if path.stat().st_size > PICTURE_BYTES:
            return None
        body = path.read_bytes()
    except OSError:
        return None
    media = MEDIA[name.rsplit(".", 1)[1].lower()]
    return HarnessPicture(scope=scope, name=name, media_type=media,  # type: ignore[arg-type]
                          data=base64.b64encode(body).decode("ascii"))


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
        activities=_activities(harness.folders()),
        hours=_hours(harness, now),
        pictures=_pictures(harness, harness.folders()),
        checked_at=stamp,
    )
