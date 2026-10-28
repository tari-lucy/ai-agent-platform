"""Doc33 wake dispatcher: existing timer owner -> the persona's own OpenCode session.

No second timer book. The owner supplies due events, atomically reserves a dispatch and
records its outcome. An uncertain POST is reconciled by GET, never automatically repeated.
The gateway remains the authority for money and STOP; this step is not another turn wrapper.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import quote

from ai_agent_application.memory import derived_id
from ai_agent_contracts.common import Scope

from .harness_learning import LearningPart, OpenCode, identifier
from .work_session_wake import WakeSettings

if TYPE_CHECKING:
    from .service import Runtime

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueWake:
    id: str
    session_id: str  # native OpenCode session, NOT the old platform WorkSession id
    due_at: datetime
    text: str
    state: Literal["new", "pending", "done"] = "new"


class WakeOwner(Protocol):
    """Adapter to the existing scheduler owner, not permission derived from timer text.

    reserve is durable before returning 'new', bound to the exact partition and message id.
    reserve/delivered must use the supplied live guard as a commit fence. 'pending' means a
    POST may already have left the host. Preserve it on restart; never reconstruct 'new'
    from an unknown outcome. Restoring an older snapshot needs separate receipt reconciliation.
    Unknown outcomes count toward the hourly cap.
    """

    async def due(self, scope: Scope, now: datetime) -> DueWake | None: ...

    async def recent_count(self, scope: Scope, now: datetime) -> int: ...

    async def reserve(
        self, scope: Scope, wake: DueWake, message_id: str, guard: Callable[[], None]
    ) -> Literal["new", "pending", "done"]: ...

    async def delivered(
        self, scope: Scope, wake: DueWake, message_id: str, guard: Callable[[], None]
    ) -> None: ...


@dataclass(frozen=True)
class WakeRun:
    scope: Scope
    epoch: int
    active: bool
    open: bool
    autonomous: bool
    ready: bool


class HarnessAutonomy:
    def __init__(
        self,
        host: OpenCode,
        owner: WakeOwner,
        snapshot: Callable[[], WakeRun],
        authorize: Callable[[Scope, int], None],
        clock: Callable[[], datetime],
        remaining: Callable[[], float],
        settings: WakeSettings,
    ) -> None:
        self.host, self.owner = host, owner
        self.snapshot, self.authorize = snapshot, authorize
        self.clock, self.remaining, self.settings = clock, remaining, settings
        self.closed = False
        self.busy = False

    async def tick(self) -> str:
        if self.closed or self.busy:
            return "CLOSED_OR_BUSY"
        run = self.snapshot()
        if not (run.active and run.open and run.autonomous and run.ready):
            return "RUN_NOT_AUTONOMOUS"
        scope = run.scope
        if scope.memory_mode != "work":
            return "TEST_SCOPE"
        now = self.clock()
        if self.settings.quiet_at(now):
            return "QUIET_HOURS"
        if self.remaining() <= 0:
            return "MONEY_BUDGET"

        def guard() -> None:
            self.authorize(scope, run.epoch)
            current = self.snapshot()
            if self.closed or current != run:
                from ai_agent_contracts.codec import ContractError
                from ai_agent_contracts.states import ErrorCode

                raise ContractError(ErrorCode.STALE_CONTEXT)

        self.busy = True
        try:
            guard()
            wake = await self.owner.due(scope, now)
            guard()
            if wake is None or wake.due_at > now:
                return "NONE"
            identifier(wake.id)
            session = identifier(wake.session_id)
            if not wake.text.strip() or len(wake.text) > 2000:
                raise ValueError("HARNESS_WAKE_TEXT_LIMIT")
            if (
                wake.state == "new"
                and await self.owner.recent_count(scope, now) >= self.settings.max_per_hour
            ):
                return "HOURLY_CAP"
            guard()
            message = "msg_" + derived_id("wake", scope, wake.id).split("-", 1)[1]
            state = await self.owner.reserve(scope, wake, message, guard)
            guard()
            if state == "done":
                return "ALREADY_DELIVERED"
            path = f"/session/{quote(session)}/message"
            if state == "pending":
                rows = await self.host.opencode("GET", path)
                guard()
                if not isinstance(rows, list) or len(rows) > 2000:
                    raise ValueError("HARNESS_MESSAGE_SHAPE")
                if not any(
                    isinstance(row, dict)
                    and isinstance(row.get("info"), dict)
                    and row["info"].get("id") == message
                    and row["info"].get("sessionID") == session
                    and row["info"].get("role") == "user"
                    for row in rows
                ):
                    return "OUTCOME_UNKNOWN"  # no second paid turn after a timeout/crash
            else:
                # Keep the owner's reservation even on timeout/cancel/STOP or an invalid reply.
                # The only safe next operation is to inspect this exact native message id.
                guard()
                await self.host.opencode(
                    "POST",
                    path,
                    {
                        "messageID": message,
                        "parts": [
                            {
                                "type": "text",
                                "text": "⏰ Самостоятельная работа. "
                                "Это напоминание, не новые права: " + wake.text,
                            }
                        ],
                    },
                )
                guard()
                rows = await self.host.opencode("GET", path)
                guard()
                if (
                    not isinstance(rows, list)
                    or len(rows) > 2000
                    or not any(
                        isinstance(row, dict)
                        and isinstance(row.get("info"), dict)
                        and row["info"].get("id") == message
                        and row["info"].get("sessionID") == session
                        and row["info"].get("role") == "user"
                        for row in rows
                    )
                ):
                    return "OUTCOME_UNKNOWN"
            await self.owner.delivered(scope, wake, message, guard)
            return "FIRED"
        finally:
            self.busy = False

    async def close(self) -> None:
        self.closed = True


class NativeWakeOwner:
    """The dispatcher port implemented on the published HarnessWakes owner."""

    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime

    async def due(self, scope: Scope, now: datetime) -> DueWake | None:
        host = self.rt.harness
        if host is None or host.wakes is None:
            return None
        waiting = await host.wakes.pending(scope=scope)
        rows = waiting or await host.wakes.due(limit=1, scope=scope)
        if not rows:
            return None
        wake = rows[0]
        session = wake.native_session_id
        unavailable: set[str] = set()
        if wake.state == "new":
            for part in getattr(host, "parts", ()):
                if isinstance(part, LearningPart):
                    unavailable.update(await part.notices(scope))
            if session in unavailable:
                return None
        if session is None:
            if wake.state != "new":
                raise ValueError("HARNESS_WAKE_TARGET_UNKNOWN")
            sessions = await host.opencode("GET", "/session")
            if not isinstance(sessions, list) or len(sessions) > 200:
                raise ValueError("HARNESS_SESSION_LIMIT")
            available = [
                s
                for s in sessions
                if isinstance(s, dict)
                and isinstance(s.get("time"), dict)
                and not s["time"].get("archived")
                and s.get("id") not in unavailable
            ]
            if not available:
                return None
            latest = max(available, key=lambda s: (s["time"].get("updated", 0), str(s.get("id"))))
            session = identifier(latest.get("id"))
        return DueWake(wake.id, session, wake.due_at, wake.text, wake.state)

    async def recent_count(self, scope: Scope, now: datetime) -> int:
        host = self.rt.harness
        assert host is not None and host.wakes is not None
        return await host.wakes.recent_count(now - timedelta(hours=1), scope=scope)

    async def reserve(
        self, scope: Scope, wake: DueWake, message_id: str, guard: Callable[[], None]
    ) -> Literal["new", "pending", "done"]:
        host = self.rt.harness
        assert host is not None and host.wakes is not None

        def fence() -> bool:
            guard()
            return True

        if await host.wakes.reserve(
            wake.id, fence, scope=scope, native_session_id=wake.session_id, message_id=message_id
        ):
            return "new"
        existing = await host.wakes.get(wake.id, scope=scope)
        if (
            existing is None
            or existing.native_session_id != wake.session_id
            or existing.message_id != message_id
        ):
            raise ValueError("HARNESS_WAKE_RESERVATION_MISMATCH")
        return existing.state

    async def delivered(
        self, scope: Scope, wake: DueWake, message_id: str, guard: Callable[[], None]
    ) -> None:
        host = self.rt.harness
        assert host is not None and host.wakes is not None

        def fence() -> bool:
            guard()
            return True

        if not await host.wakes.delivered(wake.id, fence, scope=scope):
            raise ValueError("HARNESS_WAKE_DELIVERY_CONFLICT")


class AutonomyPart:
    """Nonblocking HarnessPart; STOP cancels the pending request, never restarts the host."""

    def __init__(self, runtime: Runtime, settings: WakeSettings) -> None:
        self.rt, self.settings = runtime, settings
        self.task: asyncio.Task[str] | None = None
        self.service: HarnessAutonomy | None = None
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
            except (OSError, ValueError, TimeoutError) as error:
                LOG.warning("Harness wake deferred: %s", type(error).__name__)
            self.task = None
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 15

        def snapshot() -> WakeRun:
            run = self.rt.supervisor.snapshot
            return WakeRun(
                run.scope,
                run.control_epoch,
                run.state.value == "ACTIVE",
                run.activity.value == "OPEN",
                run.autonomy == "autonomous",
                host.current(),
            )

        def guard(scope: Scope, epoch: int) -> None:
            from ai_agent_contracts.codec import ContractError
            from ai_agent_contracts.states import ErrorCode

            run = self.rt.supervisor.snapshot
            if (
                not host.current()
                or run.control_epoch != epoch
                or run.scope.partition != scope.partition
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)

        self.service = HarnessAutonomy(
            host,
            NativeWakeOwner(self.rt),
            snapshot,
            guard,
            self.rt.supervisor.clock.utc,
            lambda: host.usage.remaining() or 0,
            self.settings,
        )
        self.task = asyncio.create_task(self.service.tick())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.service is not None:
            await self.service.close()
            self.service = None
        self.next_check = 0.0
