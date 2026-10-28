"""Doc33 wake dispatcher: existing timer owner -> the persona's own OpenCode session.

No second timer book. The owner supplies due events, atomically reserves a dispatch and
records its outcome. An uncertain POST is reconciled by GET, never automatically repeated.
The gateway remains the authority for money and STOP; this step is not another turn wrapper.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import quote

from ai_agent_application.memory import derived_id
from ai_agent_contracts.common import Scope

from .harness_learning import OpenCode, identifier
from .work_session_wake import WakeSettings


@dataclass(frozen=True)
class DueWake:
    id: str
    session_id: str  # native OpenCode session, NOT the old platform WorkSession id
    due_at: datetime
    text: str


class WakeOwner(Protocol):
    """Adapter to the existing scheduler owner, not permission derived from timer text.

    reserve is durable before returning 'new', bound to the exact partition and message id.
    reserve/delivered must use the supplied live guard as a commit fence. 'pending' means a
    POST may already have left the host. It must survive crashes and staged restore; do not
    return 'new' again for that event. Unknown outcomes count toward the hourly cap.
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
            if not wake.text.strip() or len(wake.text) > 1000:
                raise ValueError("HARNESS_WAKE_TEXT_LIMIT")
            if await self.owner.recent_count(scope, now) >= self.settings.max_per_hour:
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
                if not isinstance(rows, list):
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
            await self.owner.delivered(scope, wake, message, guard)
            return "FIRED"
        finally:
            self.busy = False

    async def close(self) -> None:
        self.closed = True
