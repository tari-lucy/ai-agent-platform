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
    NowAbility,
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
WEEK_BYTES = 3_145_728  # a week of them
PICTURES = 12
PICTURE_BYTES = 700_000  # base64 of this stays under the panel's 1 MiB reply limit
PICTURE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(jpg|jpeg|png|webp)$")
PICTURE_ID = re.compile(r"^(art|shot)--([A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(jpg|jpeg|png|webp))$")
FOLDERS = {"art": "art", "shot": "shots"}
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


def _buckets(harness: Any, start: datetime, count: int, step: timedelta, size: int) -> list[HourBucket]:
    """Counts per slot from her own journals: spoken, heard, posted, drawn."""
    slots = [{"said": 0, "heard": 0, "posted": 0, "drew": 0} for _ in range(count)]
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
        for row in _tail(path, 20000, size):
            moment = _hour(row.get("at"))
            if moment is None or not wanted(row):
                continue
            index = int((moment - start) / step)
            if 0 <= index < count:
                slots[index][key] += 1
    return [
        HourBucket(hour=(start + step * i).isoformat(timespec="seconds"), **slot)
        for i, slot in enumerate(slots)
    ]


def _hours(harness: Any, now: datetime) -> list[HourBucket]:
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    return _buckets(harness, start, 24, timedelta(hours=1), HOURS_BYTES)


def _days(harness: Any, now: datetime) -> list[HourBucket]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    return _buckets(harness, start, 7, timedelta(days=1), WEEK_BYTES)


def _imagine_log(harness: Any) -> Path | None:
    listener = getattr(harness, "listener", None)
    speaker = getattr(harness, "speaker", None)
    base = getattr(listener, "journal", None) or getattr(speaker, "journal", None)
    return None if base is None else base.parent / "imagine.jsonl"


def _pictures(harness: Any, folder: Path | None) -> list[NowPicture]:
    """Her drawings and her game shots, newest first, whatever their age: her picture shelf."""
    if folder is None:
        return []
    ideas: dict[str, str] = {}
    for row in _tail(_imagine_log(harness), 200, WEEK_BYTES):
        name = str(row.get("file", ""))
        if row.get("status") == "made" and PICTURE_NAME.fullmatch(name):
            ideas[name] = _clean(row.get("scene") or row.get("meaning") or "", 200)
    found: list[tuple[float, NowPicture]] = []
    for kind, directory in FOLDERS.items():
        root = folder / directory
        try:
            if root.is_symlink() or not root.is_dir():
                continue
            entries = list(root.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            if (
                path.is_symlink()
                or not path.is_file()
                or not PICTURE_NAME.fullmatch(path.name)
                or stat.st_size > PICTURE_BYTES
            ):
                continue
            caption = ideas.get(path.name, "") if kind == "art" else ""
            caption = caption or path.stem.replace("_", " ").replace("-", " ")[:120]
            found.append((stat.st_mtime, NowPicture(
                id=f"{kind}--{path.name}", kind=kind, at=_iso(stat.st_mtime), caption=caption,  # type: ignore[arg-type]
            )))
    found.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in found[:PICTURES]]


def picture(harness: Any, scope: Scope, picture_id: str) -> HarnessPicture | None:
    """One picture by id; only an id that the shelf lists is served, never an arbitrary path."""
    match = PICTURE_ID.fullmatch(picture_id) if harness is not None else None
    folder = None if match is None else harness.folders()
    if match is None or folder is None or picture_id not in {p.id for p in _pictures(harness, folder)}:
        return None
    path = folder / FOLDERS[match.group(1)] / match.group(2)
    try:
        if path.stat().st_size > PICTURE_BYTES:
            return None
        body = path.read_bytes()
    except OSError:
        return None
    media = MEDIA[match.group(3).lower()]
    return HarnessPicture(scope=scope, id=picture_id, media_type=media,  # type: ignore[arg-type]
                          data=base64.b64encode(body).decode("ascii"))


def _scripts(folder: Path | None) -> list[str]:
    """Names of the game scripts in her skills folder: what she wrote or was given to use."""
    if folder is None:
        return []
    root = folder / "skills" / "minecraft"
    try:
        if root.is_symlink() or not root.is_dir():
            return []
        return sorted(p.stem for p in root.iterdir() if p.suffix == ".py" and p.is_file())[:24]
    except OSError:
        return []


def _abilities(harness: Any) -> list[NowAbility]:
    """What she can do and with which tools, from the same conditions that hand her the tools."""
    from .harness_game import GameSource
    from .harness_watch import Watch

    game: Any = None
    for part in harness.parts:
        if isinstance(part, Watch):
            game = next((s for s in part.sources if isinstance(s, GameSource)), None)
    scripts = _scripts(harness.folders())
    listener = getattr(harness, "listener", None)
    brain = getattr(harness, "brain", None)

    def row(title: str, wired: bool, on: bool, detail: str, *tools: str) -> NowAbility:
        return NowAbility(title=title, state="on" if wired and on else "idle" if wired else "off",
                          detail=detail[:300], tools=list(tools))

    in_game = bool(game is not None and getattr(game, "in_game", False))
    return [
        row("Играть в Minecraft", game is not None, in_game,
            "Ходит, смотрит, читает таблички, строит по схеме. "
            + ("Сейчас в мире." if in_game else "Сейчас не в мире.")
            + (f" Её скрипты: {', '.join(scripts[:10])}." if scripts else ""),
            "mod: 70 действий", "orient", "read_sign", "blueprint"),
        row("Говорить голосом", harness.speaker is not None, harness.speaker is not None,
            "Голос уходит в микрофон игры, стрима или звонка; аватар двигает ртом.", "speak"),
        row("Слушать", listener is not None, bool(getattr(listener, "on", False)),
            "Слышит речь, отвечает, когда обратились по имени.", "listen"),
        row("Писать игрокам в чат", bool(getattr(brain, "game_say", None)), True,
            "Отвечает в игровом чате; быстрая голова подхватывает разговор за секунды.", "game_say"),
        row("Быстрая голова", brain is not None, True,
            "Отвечает сразу, пока основной ум занят делом, и передаёт ему поручения.", "voice_brief"),
        row("Видеть картинки", harness.look is not None, True,
            "Разглядывает кадры игры и присланные файлы.", "look"),
        row("Рисовать", harness.imagine is not None, True, "Генерирует картинки по замыслу.", "imagine"),
        row("Расшифровывать запись", harness.hear is not None, True,
            "Превращает аудио из папки inbox в текст.", "transcribe"),
        row("Аватар", harness.avatar is not None, getattr(harness.avatar, "state", "") == "on",
            "Выражения лица и движение рта.", "avatar_express"),
        row("Стрим и чаты", harness.stage is not None or harness.chats is not None,
            getattr(harness.stage, "state", "") == "on" or bool(getattr(harness.chats, "connected", None)),
            "Переключает сцены OBS, читает чат стрима и отвечает зрителям.",
            "stream_status", "stream_scene", "chat_read", "chat_say"),
        row("Канал в Telegram", harness.channel is not None, True,
            "Публикует посты и отвечает на комментарии.", "channel_post", "channel_comments", "channel_reply"),
        row("Помнить людей", harness.people is not None, True,
            "Знает, кого встречала, и что о них известно.", "person_find", "person_recall", "person_note"),
        row("Память и запросы", True, True,
            "Вспоминает прошлое, ставит себе будильники, просит у вас доступ и инструменты.",
            "memory_recall", "persona_brief", "wake_me", "ticket_raise"),
    ]


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
    from .harness_autonomy import AutonomyPart
    from .harness_limit import describe

    asleep = None
    for part in harness.parts:
        if isinstance(part, AutonomyPart) and part.sleeping() is not None:
            asleep = describe(part.sleeping(), now)  # the plan ran out: she sleeps until the window opens
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
    if asleep:
        doing = asleep
    return HarnessNow(
        scope=scope,
        enabled=True,
        doing=doing,
        recently=recently,
        goals=_goals(harness.folders()),
        lines=lines[:MAX_LINES],
        activities=_activities(harness.folders()),
        hours=_hours(harness, now),
        days=_days(harness, now),
        abilities=_abilities(harness),
        pictures=_pictures(harness, harness.folders()),
        checked_at=stamp,
    )
