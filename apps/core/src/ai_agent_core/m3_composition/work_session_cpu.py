"""W7b daily CPU budget of the persona's own work: a local journal and the wake-up check.

Each work turn reports the CPU its sandbox used (the container's own cgroup counter; a resident
sandbox adds background work since the previous turn). The host appends it to
``<root>/data/usage/YYYY-MM-DD.jsonl``. Over the daily budget the agent's own wake-ups wait
for the next UTC day; turns the operator starts are not held back by it.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class CpuSettings:
    log_dir: Path
    daily_seconds: int | None = None


def writer(settings: CpuSettings, persona_id: str, attempt_id: str) -> Callable[[float], None]:
    def record(seconds: float) -> None:
        now = datetime.now(UTC)
        settings.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = settings.log_dir / (now.strftime("%Y-%m-%d") + ".jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        line = {
            "at": now.isoformat(),
            "persona": persona_id,
            "attempt": attempt_id,
            "cpu_seconds": round(seconds, 3),
        }
        with os.fdopen(os.open(path, flags, 0o600), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(line) + "\n")

    return record


def used_today(settings: CpuSettings, persona_id: str, now: datetime | None = None) -> float:
    path = settings.log_dir / ((now or datetime.now(UTC)).strftime("%Y-%m-%d") + ".jsonl")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()
    except FileNotFoundError:
        return 0.0
    total = 0.0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("persona") != persona_id:
            continue
        value = row.get("cpu_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def exhausted(settings: CpuSettings | None, persona_id: str) -> bool:
    return (
        settings is not None
        and settings.daily_seconds is not None
        and used_today(settings, persona_id) >= settings.daily_seconds
    )
