"""The drive: while she is in the world and has stopped, give her the next push.

Without it she finishes a step and waits for the operator. This source watches (no model) whether
her session is idle; when it has been idle for a while and the bot is in a world, it says so as a
short event with the next step, and the watcher turns that into one wake. The delay grows when a
push leads to nothing (she answers in a line and stops) and returns to the base when a push starts
real work, so an empty world does not eat the budget. A day's count is capped; the day's money,
STOP and the quiet rules stay with the watcher and the dispatcher. Local state, never in Git.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from ai_agent_contracts.common import Scope

from .harness_events import Event, Level

LOG = logging.getLogger(__name__)

PUSH_TEXT = (
    "You are in the world and idle. Do the next step of your plan without asking the operator. "
    "If servers/<name>.md lists fewer than three main places, first learn the server: run /warp "
    "or /warps and read the answer, open the hotbar menu (compass, star), read the signs at the "
    "spawn, ask the players where the best places are, and write the places into the profile. "
    "Otherwise pick a place you have not visited (main places first), go there (tour.py), shoot "
    "the entrance, look around, note it in ideas.md. One concrete action now."
)
WORKED = 90.0  # a run of at least this many seconds after a push counts as real work


class IdleSource:
    name = "idle"

    def __init__(
        self,
        runtime: Any,
        game: Any,
        base: float = 180.0,
        cap: float = 1800.0,
        per_day: int = 40,
        interval: float = 15.0,
        clock: Callable[[], float] | None = None,
        today: Callable[[], str] | None = None,
    ) -> None:
        self.rt, self.game, self.interval = runtime, game, interval
        self.base, self.cap, self.per_day = base, cap, per_day
        self.clock = clock or time.monotonic
        self.today = today or (lambda: time.strftime("%Y-%m-%d"))
        self.delay = base
        self.idle_since: float | None = None
        self.run_start: float | None = None
        self.last_push = -1e9
        self.pushed = 0
        self.day = self.today()
        self.judged = True  # the last push has been judged already

    async def is_busy(self) -> bool:
        host = self.rt.harness
        rows = await host.opencode("GET", "/session/status")
        if not isinstance(rows, dict):
            return False
        return any(
            isinstance(v, dict) and v.get("type") in ("busy", "retry") for v in rows.values()
        )

    async def poll(self, scope: Scope, current: Callable[[], None]) -> list[Event]:
        if not self.game.in_game:
            self.idle_since = self.run_start = None
            return []
        now = self.clock()
        if self.today() != self.day:
            self.day, self.pushed, self.delay = self.today(), 0, self.base
        busy = await self.is_busy()
        current()
        if busy:
            if self.run_start is None:
                self.run_start = now
            self.idle_since = None
            return []
        if self.idle_since is None:
            self.idle_since = now
            if self.run_start is not None:  # a run of hers has just ended
                if not self.judged and self.run_start >= self.last_push:
                    long = now - self.run_start >= WORKED
                    # A push that started real work resets the pause; a one-line answer doubles it.
                    self.delay = self.base if long else min(self.delay * 2, self.cap)
                    self.judged = True
                self.run_start = None
        if self.pushed >= self.per_day:
            return []
        if now - self.idle_since < self.delay or now - self.last_push < self.delay:
            return []
        self.last_push, self.judged = now, False
        self.pushed += 1
        return [Event("idle", "push", "play", PUSH_TEXT, Level.HIGH)]

    async def delivered(self, scope: Scope, current: Callable[[], None]) -> None:
        return None
