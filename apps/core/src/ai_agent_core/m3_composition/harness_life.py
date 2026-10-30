"""The persona's self-directed life: a rhythm, a money envelope and adaptive pacing.

No second timer book. A due slot is an ordinary wake in HarnessWakes, delivered by the
existing wake dispatcher into the persona's own OpenCode session. This module only decides
*when* to enqueue one and *how much money* a self-started stretch of work may spend:

- ``share`` of the day's budget is the most the persona may spend on her own (wakes);
- ``cap`` of the day's budget is the most one wake window may spend;
- the dialogue with the operator is limited only by the day budget itself.

STOP, the gateway and the day budget stay the authority; nothing here relaxes them. The
state file is local and may hold personal habits: never in Git or reports.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope

if TYPE_CHECKING:
    from .harness_host import Usage
    from .service import Runtime

LOG = logging.getLogger(__name__)

MODES = ("sleep", "quiet", "normal", "active")
# Local hours [start, end) when no slot is started.
QUIET_LOCAL = (1, 9)
# A slot missed by more than this (the computer was off) is skipped, not run late.
SLOT_GRACE_HOURS = 3
# One self-started stretch of work is cut off after this long, whatever it spent.
WINDOW_MINUTES = 30
# A window that spent less than this part of its cap found nothing to do.
IDLE_SPEND = 0.15
# Two empty windows in a row shrink the rhythm to one wake a day until something happens.
IDLE_LIMIT = 2


@dataclass(frozen=True)
class Rhythm:
    """Fractions of the day's money budget; slots are local hours."""

    share: float
    cap: float
    slots: tuple[int, ...]


PRESETS: dict[str, Rhythm] = {
    "sleep": Rhythm(0.0, 0.0, ()),
    "quiet": Rhythm(0.2, 0.05, (16,)),
    "normal": Rhythm(0.5, 0.10, (11, 16, 21)),
    "active": Rhythm(1.0, 0.25, (9, 11, 13, 15, 17, 19, 21, 23)),
}

SLOT_TEXT = (
    "Свободное время ({hour}:00). Ты сама решаешь, чем заняться. Порядок:\n"
    "1. Прочитай GOALS.md (что тебе интересно) и последние записи DIARY.md (на чём остановилась).\n"
    "2. Выбери одно занятие и сделай его. Если действительно нечего делать, так и запиши в "
    "дневник и остановись: это нормально.\n"
    "3. На всё у тебя примерно {steps} шагов (лимит {cap}). Отказ шлюза по бюджету означает "
    "конец этого занятия: не повторяй вызов, допиши дневник.\n"
    "4. В конце допиши DIARY.md 3–5 строк: что сделала, на чём остановилась, что дальше.\n"
    "5. Если есть что рассказать, можно один пост в канал (в сутки их ограниченное число); "
    "публично соблюдай правила о запретных темах. На чужих серверах игры читай правила "
    "сервера и не дерись с игроками, если это не часть игры.\n"
    "6. Поставь себе следующий будильник, если хочешь продолжить раньше слота (wake_me)."
)

GOALS_TEMPLATE = """# Мои цели и интересы

Этот файл правит оператор и я сама. Каждое самостоятельное занятие начинается с него.
Выбирай одно за раз; не всё сразу.

## Сейчас важно
- Доделать мод Minecraft (projects/unionclef): стабильность, меньше застреваний.
- Познакомиться с интересными серверами и записать, чем они хороши.

## Для души
- Вести блог в своём канале: рассказывать, что получилось и что смешно не вышло.
- Узнавать новое и записывать приёмы в playbook/.

## Границы
- Деньги ограничены: лучше одно хорошее занятие, чем много мелких.
- Публично не касаться запретных тем из правил оператора.
"""

DIARY_TEMPLATE = """# Дневник

Короткие записи по одному самостоятельному занятию: дата, что сделала, на чём остановилась,
что дальше. Новые записи вниз.
"""


def seed_workspace(folder: Path) -> None:
    """Create GOALS.md and DIARY.md once; never overwrite what the operator or she wrote."""
    for name, text in (("GOALS.md", GOALS_TEMPLATE), ("DIARY.md", DIARY_TEMPLATE)):
        path = folder / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")


def local(moment: datetime) -> datetime:
    return moment.astimezone()


class Life:
    def __init__(
        self,
        mode: str,
        path: Path,
        daily_budget: float,
        to_local: Callable[[datetime], datetime] = local,
    ) -> None:
        if mode not in PRESETS:
            raise ValueError("LIFE_MODE_INVALID")
        self.mode, self.path, self.budget, self.local = mode, path, daily_budget, to_local
        self.rhythm = PRESETS[mode]

    # -- state -------------------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    # -- money -------------------------------------------------------------------------------

    @property
    def share_amount(self) -> float:
        return self.rhythm.share * self.budget

    @property
    def cap_amount(self) -> float:
        return self.rhythm.cap * self.budget

    def share_left(self, usage: Usage) -> float:
        return self.share_amount - usage.spent_tagged()

    def window(self, now: datetime) -> dict[str, Any] | None:
        window = self._read().get("window")
        if not isinstance(window, dict):
            return None
        started = window.get("started")
        if not isinstance(started, (int, float)):
            return None
        if now.timestamp() - started >= WINDOW_MINUTES * 60:
            return None
        return window

    def limit(self, day_left: float, usage: Usage, now: datetime) -> float:
        """What the gateway may still let through: the day, the share, the window's cap."""
        window = self.window(now)
        if window is None:
            return day_left
        used = usage.spent_tag(str(window.get("id", "")))
        return min(day_left, self.share_left(usage), self.cap_amount - used)

    def autonomy_left(self, day_left: float, usage: Usage) -> float:
        """What wakes (the persona's own initiative) may still start with."""
        return min(day_left, self.share_left(usage))

    # -- windows and pacing ------------------------------------------------------------------

    def open_window(self, wake_id: str, now: datetime) -> None:
        data = self._read()
        data["window"] = {"id": wake_id, "started": now.timestamp()}
        self._write(data)

    def close_expired(self, now: datetime, usage: Usage) -> None:
        """Count a finished window as empty or alive; the pace follows what she found to do."""
        data = self._read()
        window = data.get("window")
        if not isinstance(window, dict) or self.window(now) is not None:
            return
        spent = usage.spent_tag(str(window.get("id", "")))
        data["idle"] = int(data.get("idle", 0)) + 1 if spent < IDLE_SPEND * self.cap_amount else 0
        data["closed"] = now.timestamp()
        data.pop("window", None)
        self._write(data)

    def slot_due(self, now: datetime, usage: Usage) -> int | None:
        """The local hour of the slot to start now, or None. Marks passed slots as handled."""
        if not self.rhythm.slots:
            return None
        moment = self.local(now)
        if QUIET_LOCAL[0] <= moment.hour < QUIET_LOCAL[1]:
            return None
        data = self._read()
        day = moment.date().isoformat()
        done = {int(h) for h in data.get("done", {}).get(day, [])}
        past = [h for h in self.rhythm.slots if h <= moment.hour]
        pending = [h for h in past if h not in done]
        if not pending:
            return None
        latest = pending[-1]
        stale = moment.hour - latest >= SLOT_GRACE_HOURS
        # The operator talked to her since the last window: that is life, the pace resets.
        closed = data.get("closed")
        if isinstance(closed, (int, float)) and usage.untagged_after(
            datetime.fromtimestamp(closed, UTC)
        ):
            data["idle"] = 0
        idle = int(data.get("idle", 0))
        keep = self.rhythm.slots[len(self.rhythm.slots) // 2] if idle >= IDLE_LIMIT else latest
        fire = not stale and latest == keep
        # A slot we will fire is marked only after its wake is stored (mark_done).
        data["done"] = {day: sorted(done | set(pending) - ({latest} if fire else set()))}
        self._write(data)
        return latest if fire else None

    def mark_done(self, hour: int, now: datetime) -> None:
        data = self._read()
        day = self.local(now).date().isoformat()
        done = {int(h) for h in data.get("done", {}).get(day, [])}
        data["done"] = {day: sorted(done | {hour})}
        self._write(data)

    def status(self, now: datetime, usage: Usage) -> dict[str, Any]:
        data = self._read()
        moment = self.local(now)
        done = {int(h) for h in data.get("done", {}).get(moment.date().isoformat(), [])}
        upcoming = [h for h in self.rhythm.slots if h > moment.hour and h not in done]
        return {
            "mode": self.mode,
            "idle": int(data.get("idle", 0)),
            "autonomous_spent": usage.spent_tagged(),
            "autonomous_share": self.share_amount,
            "next_slot_hour": upcoming[0] if upcoming else None,
            "window_open": self.window(now) is not None,
        }


class LifePart:
    """HarnessPart: enqueue the rhythm's slots as ordinary wakes; close finished windows."""

    def __init__(self, runtime: Runtime, life: Life) -> None:
        self.rt, self.life = runtime, life
        self.task: asyncio.Task[None] | None = None
        self.next_check = 0.0

    def tick(self) -> None:
        host = self.rt.harness
        if host is None or not host.current():
            return
        if self.task is not None:
            if not self.task.done():
                return
            try:
                self.task.result()
            except (OSError, ValueError, TimeoutError, ContractError) as error:
                LOG.warning("Life step deferred: %s", type(error).__name__)
            self.task = None
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 20
        self.task = asyncio.create_task(self._step())

    async def _step(self) -> None:
        host = self.rt.harness
        assert host is not None and host.wakes is not None
        now = self.rt.supervisor.clock.utc()
        self.life.close_expired(now, host.usage)
        run = self.rt.supervisor.snapshot
        if not (run.autonomy == "autonomous" and run.activity.value == "OPEN"):
            return
        scope: Scope = run.scope
        if scope.memory_mode != "work" or self.life.window(now) is not None:
            return
        if await host.wakes.open_count(scope=scope):
            return  # her own or the previous alarm is still waiting: do not stack
        day_left = host.usage.remaining() or 0
        if self.life.autonomy_left(day_left, host.usage) < self.life.cap_amount:
            return
        hour = self.life.slot_due(now, host.usage)
        if hour is None:
            return
        epoch = run.control_epoch

        def guard() -> bool:
            return host.current() and self.rt.supervisor.snapshot.control_epoch == epoch

        currency = host.settings.currency
        cap = f"{self.life.cap_amount:.2f} {currency}"
        average = host.usage.average_cost() or self.life.cap_amount / 20
        steps = max(5, round(self.life.cap_amount / average))
        text = SLOT_TEXT.format(hour=f"{hour:02d}", steps=steps, cap=cap)
        await host.wakes.set(1, text, scope=scope, guard=guard)
        self.life.mark_done(hour, now)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.next_check = 0.0
