"""W3 internet for a work turn: the permission check, the gateway factory and its local log.

The log holds full HTTP URLs and HTTPS host:port of the agent's requests. It may contain
personal data: it stays in the host's private data directory, never in Git or reports.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import EgressLog, EgressRequest

GUIDE = (
    "Интернет в этом ходе доступен через шлюз платформы: переменные HTTP_PROXY/HTTPS_PROXY "
    "уже настроены, pip, npm, curl и webfetch их используют. Своего DNS и прямого маршрута "
    "наружу нет — всё идёт через шлюз. Локальная сеть, компьютер оператора, другие агенты, "
    "адреса метаданных и почтовые порты закрыты; отказ шлюза (403 EGRESS_…) — граница, "
    "а не сбой, обходить её не нужно. Каждый запрос пишется в журнал (оператор видит его "
    "в панели), объём за ход и за сутки ограничен: отказ EGRESS_DAILY_LIMIT действует до "
    "следующих суток UTC, расширение — обращением. STOP рвёт соединения. Не отправляй в сеть "
    "личное из памяти и разговоров без явной просьбы оператора. Публикации от имени "
    "персонажа (каналы, соцсети) — только через подключения платформы, не напрямую. "
)


@dataclass(frozen=True)
class EgressSettings:
    capability_id: str
    image_id: str
    limits: EgressLimits
    log_dir: Path | None = None
    # Bytes per persona per UTC day, counted from the log; None: only the per-turn limit.
    daily_bytes: int | None = None


def log_writer(
    settings: EgressSettings, persona_id: str, attempt_id: str
) -> Callable[[dict[str, Any]], None]:
    """Append-only JSON lines per day; the directory is created owner-only on first use."""

    def record(row: dict[str, Any]) -> None:
        if settings.log_dir is None:
            return
        now = datetime.now(UTC)
        line = {
            "at": now.isoformat(),
            "persona": persona_id,
            "attempt": attempt_id,
            **{key: row[key] for key in sorted(row) if key != "at"},
        }
        settings.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = settings.log_dir / (now.strftime("%Y-%m-%d") + ".jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")

    return record


def rows(log_dir: Path, day: str) -> list[dict[str, Any]]:
    """One day's log lines; unreadable or foreign lines are skipped, never trusted further."""
    path = log_dir / (day + ".jsonl")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except FileNotFoundError:
        return []
    found = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            found.append(row)
    return found


def used_today(settings: EgressSettings, persona_id: str, now: datetime | None = None) -> int:
    if settings.log_dir is None:
        return 0
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    total = 0
    for row in rows(settings.log_dir, day):
        if row.get("persona") == persona_id:
            total += sum(value for key in ("up", "down") if isinstance(value := row.get(key), int))
    return total


def factory(settings: EgressSettings, admission: WorkAdmission) -> Callable[[], Egress]:
    return resident_factory(settings, admission.scope.persona_id, admission.attempt_id)


def resident_factory(
    settings: EgressSettings, persona_id: str, attempt_id: str = "resident"
) -> Callable[[], Egress]:
    """A gateway for a kept sandbox: it serves several turns, its log says ``resident``."""
    record = log_writer(settings, persona_id, attempt_id)

    def make() -> Egress:
        remaining = None
        if settings.daily_bytes is not None:
            remaining = settings.daily_bytes - used_today(settings, persona_id)
        return Egress(settings.image_id, settings.limits, record, remaining)

    return make


def log_view(settings: EgressSettings | None, scope: Scope, day: str) -> EgressLog:
    """The operator's view of one UTC day: totals and the newest 200 requests of this persona.

    Lines that do not fit the contract (foreign, damaged) are counted out, not shown.
    """
    now = datetime.now(UTC)
    datetime.strptime(day, "%Y-%m-%d")  # ValueError for a malformed day
    found = (
        [r for r in rows(settings.log_dir, day) if r.get("persona") == scope.persona_id]
        if settings is not None and settings.log_dir is not None
        else []
    )
    items: list[EgressRequest] = []
    used = denied = 0
    for row in found:
        used += sum(value for key in ("up", "down") if isinstance(value := row.get(key), int))
        denied += row.get("status") == "denied"
    for row in reversed(found):
        if len(items) == 200:
            break
        try:
            items.append(
                EgressRequest.model_validate_json(
                    json.dumps({key: row[key] for key in EgressRequest.model_fields if key in row})
                )
            )
        except ValueError:
            continue
    return EgressLog(
        scope=scope,
        enabled=settings is not None,
        day=day,
        used_bytes=used,
        daily_limit_bytes=settings.daily_bytes if settings is not None else None,
        turn_limit_bytes=settings.limits.max_bytes if settings is not None else None,
        requests=len(found),
        denied=denied,
        items=items,
        complete=len(items) == len(found),
        checked_at=now,
    )
