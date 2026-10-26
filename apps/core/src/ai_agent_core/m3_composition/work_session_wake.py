"""W7 self-directed work: the agent's own wake-ups and its diary conversation.

``wake_me`` (a workspace operation of a running turn) stores a ``WakeTimer`` with the activity
owner. The runtime tick fires a due timer only while the run is ACTIVE, OPEN and autonomous,
in work memory, outside the operator's quiet hours and within the hourly cap: its text is
stored by the memory Source owner and appended as a ``wake`` message to the diary
conversation, which the ordinary session driver then answers (same budget, rights, STOP).
Nothing fires while STOPPED; a restart never fires anything before START.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ai_agent_application.agent_activity.sessions import SessionBook
from ai_agent_application.memory import SourceDraft, derived_id
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import area
from ai_agent_contracts.agent_sessions import WakeTimer
from ai_agent_contracts.api import CreateCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.states import ErrorCode, Provenance

from .dialogue import memory
from .state import PRINCIPALS, command, extension, record
from .work_sessions import ADAPTER, SOURCE_TYPE
from .work_sessions import sessions as reader

if TYPE_CHECKING:
    from collections.abc import Callable

    from .service import Runtime

LOG = logging.getLogger(__name__)
DIARY = "Самостоятельная работа"
MAX_MINUTES = 7 * 24 * 60
CHECK_SECONDS = 15.0
# Done markers are read from the persona's home by a short helper container: at most this often.
MARKER_SECONDS = 60.0
MARKERS = ".platform/done"
MARKER = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
GUIDE = (
    "Ты можешь сам назначить себе следующий шаг: операция workspace wake_me с аргументами "
    "minutes (строка, от 1 до 10080) и reason (что именно продолжить, до 500 символов) "
    "ставит будильник. В назначенное время платформа напишет в разговор «Самостоятельная "
    "работа», и ты продолжишь там с этим текстом: память хода не переносится, поэтому "
    "пиши в reason всё нужное и веди план в /workspace. Будильник срабатывает, только когда "
    "оператор запустил тебя в автономном режиме, вне тихих часов и не чаще заданного числа "
    "раз в час; прав он не выдаёт. Важное сообщай оператору, новый доступ — обращением. "
    "Чтобы проснуться, когда закончится долгая фоновая работа (сборка, рендер), запусти её "
    "так: nohup sh -c 'КОМАНДА > /workspace/scratch/имя.log 2>&1; echo $? > "
    "/workspace/.platform/done/имя' & — и поставь wake_me с when_done=имя (латиница, цифры, "
    "дефис): будильник сработает по появлении файла (проверка раз в минуту) или через "
    "minutes, что раньше. Фоновая работа переживает ход только в долгоживущем контейнере. "
)


@dataclass(frozen=True)
class WakeSettings:
    max_per_hour: int = 4
    # Local hours [start, end) of the host when nothing fires; None: no quiet hours.
    quiet: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_per_hour <= 60 or (
            self.quiet is not None and not all(0 <= hour <= 23 for hour in self.quiet)
        ):
            raise ValueError("WAKE_SETTINGS_INVALID")

    def quiet_at(self, now: datetime) -> bool:
        if self.quiet is None or self.quiet[0] == self.quiet[1]:
            return False
        hour, (start, end) = now.astimezone().hour, self.quiet
        return start <= hour < end if start < end else hour >= start or hour < end


def book(rt: Runtime, authorize: Callable[[Scope], None]) -> SessionBook:
    base = reader(rt)
    return SessionBook(
        rt.store,
        PRINCIPALS[Owner.ACTIVITY],
        rt.supervisor.clock.utc,
        authorize=authorize,
        verify_source=base.verify_source,
        readers=rt.readers,
    )


async def diary(rt: Runtime, sessions: SessionBook, scope: Scope) -> str:
    """The open diary conversation, opened once; a closed one is followed by a new one."""
    rows = await sessions.sessions(scope)
    found = [row for row in rows if row.title == DIARY and row.state in ("open", "waiting")]
    if found:
        return min(found, key=lambda row: row.created_at).id
    closed = sum(row.title == DIARY for row in rows)
    request = record(
        rt.registry,
        CreateCommand,
        request_id=derived_id("wake-diary", area(scope), str(closed)),
        scope=area(scope),
        type="work.session.open",
        target="work",
        arguments=extension(rt.registry, "work.session.open-args", {"title": DIARY}),
    )
    receipt = await sessions.open(request)
    assert receipt.resource_id is not None
    return receipt.resource_id


async def wake_me(
    rt: Runtime,
    scope: Scope,
    guard: Callable[[], None],
    key: str,
    minutes: str,
    reason: str,
    when_done: str = "",
) -> dict[str, str]:
    """The workspace operation: bounded delay and text; the timer is data, not a right."""
    if not minutes.isascii() or not minutes.isdigit() or len(minutes) > 6:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    if when_done and (MARKER.fullmatch(when_done) is None or rt.session_home_files is None):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    delay = int(minutes)
    reason = reason.strip()
    if not 1 <= delay <= MAX_MINUTES or not reason or len(reason) > 500:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    def authorize(stable: Scope) -> None:
        guard()
        if stable.partition != scope.partition:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    sessions = book(rt, authorize)
    session_id = await diary(rt, sessions, scope)
    timer = await sessions.set_timer(scope, key, session_id, delay, reason, when_done or None)
    answer = {"timer_id": timer.id, "due_at": timer.due_at.isoformat(), "conversation": DIARY}
    if timer.done_marker is not None:
        answer["when_done"] = "/workspace/" + MARKERS + "/" + timer.done_marker
    return answer


class AutonomousWake:
    """The runtime tick's wake step: at most one due timer per call; usually a no-op."""

    def __init__(self, runtime: Runtime, settings: WakeSettings) -> None:
        self.rt, self.settings = runtime, settings
        self.next_check = 0.0
        self.next_markers = 0.0

    async def finished(self, scope: Scope, timers: list[WakeTimer]) -> WakeTimer | None:
        """A pending marker timer whose marker appeared after it was set; None when unknown."""
        files = self.rt.session_home_files
        waiting = [t for t in timers if t.state == "pending" and t.done_marker is not None]
        now = time.monotonic()
        if not waiting or files is None or now < self.next_markers:
            return None
        self.next_markers = now + MARKER_SECONDS
        from .work_session_home import volume

        try:
            listed = await files.list(volume(scope), MARKERS)
        except OSError, TimeoutError, ValueError:
            LOG.info("Wake markers unreadable; timers wait for their time")
            return None
        if listed.get("status") != "ok":
            return None
        seen = {
            row["name"]: row["mtime"]
            for row in listed.get("entries", [])
            if row.get("type") == "file" and isinstance(row.get("mtime"), int)
        }
        for timer in waiting:
            written = seen.get(timer.done_marker or "")
            # Whole seconds there: a marker of the same second still counts.
            if written is not None and written >= int(timer.created_at.timestamp()):
                return timer
        return None

    async def tick(self) -> str:
        rt = self.rt
        run = rt.supervisor.snapshot
        if (
            run.state.value != "ACTIVE"
            or run.activity.value != "OPEN"
            or run.autonomy != "autonomous"
            or run.scope.memory_mode != "work"
        ):
            return "RUN_NOT_AUTONOMOUS"
        elapsed = rt.supervisor.clock.elapsed()
        if elapsed < self.next_check:
            return "THROTTLED"
        self.next_check = elapsed + CHECK_SECONDS
        now = rt.supervisor.clock.utc()
        if self.settings.quiet_at(now):
            return "QUIET_HOURS"
        scope = area(run.scope)

        def authorize(stable: Scope) -> None:
            snapshot = rt.supervisor.snapshot
            if snapshot.state.value != "ACTIVE" or stable.partition != scope.partition:
                raise ContractError(ErrorCode.ACCESS_DENIED)

        sessions = book(rt, authorize)
        timers = await sessions.timers(scope)
        recent = [
            t for t in timers if t.state == "fired" and now - t.updated_at < timedelta(hours=1)
        ]
        if len(recent) >= self.settings.max_per_hour:
            return "HOURLY_CAP"
        due = [t for t in timers if t.state == "pending" and t.due_at <= now]
        done = None if due else await self.finished(scope, timers)
        if not due and done is None:
            return "NONE"
        timer = due[0] if due else done
        assert timer is not None
        why = (
            "фоновая работа завершилась (/workspace/" + MARKERS + "/" + timer.done_marker + ")"
            if done is not None and timer.done_marker is not None
            else "наступило назначенное время"
            + (", файл завершения ещё не появился" if timer.done_marker is not None else "")
        )
        text = (
            "Будильник, который ты поставил себе сам "
            f"({timer.created_at:%Y-%m-%d %H:%M} UTC; {why}): {timer.reason}"
        )
        made = await memory(rt).register_source(
            command(scope, "wake-source-" + timer.id),
            SourceDraft(
                provenance=Provenance.MODEL_PROPOSAL,
                source_type=SOURCE_TYPE,
                adapter_id=ADAPTER,
                adapter_version="1",
                occurred_at=now,
                content=text,
                read_allowed=True,
                api_transfer_allowed=True,  # the agent's own note, back to its own model
                public_disclosure_allowed=False,
                external_id=derived_id("wake-message", scope, timer.id),
                author_id="agent",
            ),
        )
        ref = SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )
        sequence = await sessions.wake(scope, timer, ref, text)
        if not sequence:
            LOG.info("Wake timer dropped: its conversation is closed")
            return "DROPPED"
        rt.wake_activity(scope)
        rt.notify("work-session.changed", scope, timer.session_id, sequence)
        return "FIRED"
