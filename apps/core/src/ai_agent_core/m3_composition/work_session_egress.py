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

GUIDE = (
    "Интернет в этом ходе доступен через шлюз платформы: переменные HTTP_PROXY/HTTPS_PROXY "
    "уже настроены, pip, npm, curl и webfetch их используют. Своего DNS и прямого маршрута "
    "наружу нет — всё идёт через шлюз. Локальная сеть, компьютер оператора, другие агенты, "
    "адреса метаданных и почтовые порты закрыты; отказ шлюза (403 EGRESS_…) — граница, "
    "а не сбой, обходить её не нужно. Каждый запрос пишется в журнал, объём за ход "
    "ограничен, STOP рвёт соединения. Не отправляй в сеть личное из памяти и разговоров "
    "без явной просьбы оператора. Публикации от имени персонажа (каналы, соцсети) — только "
    "через подключения платформы, не напрямую. "
)


@dataclass(frozen=True)
class EgressSettings:
    capability_id: str
    image_id: str
    limits: EgressLimits
    log_dir: Path | None = None


def log_writer(
    settings: EgressSettings, admission: WorkAdmission
) -> Callable[[dict[str, Any]], None]:
    """Append-only JSON lines per day; the directory is created owner-only on first use."""

    def record(row: dict[str, Any]) -> None:
        if settings.log_dir is None:
            return
        now = datetime.now(UTC)
        line = {
            "at": now.isoformat(),
            "persona": admission.scope.persona_id,
            "attempt": admission.attempt_id,
            **{key: row[key] for key in sorted(row) if key != "at"},
        }
        settings.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = settings.log_dir / (now.strftime("%Y-%m-%d") + ".jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")

    return record


def factory(settings: EgressSettings, admission: WorkAdmission) -> Callable[[], Egress]:
    record = log_writer(settings, admission)
    return lambda: Egress(settings.image_id, settings.limits, record)
