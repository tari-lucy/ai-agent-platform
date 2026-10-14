"""Monotonic GM time: downtime, pause and sleep gaps never spend cooldown."""

import math
from collections.abc import Callable

from ai_agent_contracts.control import RunSession
from ai_agent_contracts.states import ActivityState, RunState


class ActiveGMClock:
    def __init__(
        self, elapsed: Callable[[], float], *, initial: float = 0.0, max_gap: float = 60.0
    ):
        if not math.isfinite(initial) or initial < 0 or not 0 < max_gap <= 3600:
            raise ValueError("GM_CLOCK_INVALID")
        self.elapsed, self.seconds, self.max_gap = elapsed, initial, max_gap
        self.last = elapsed()
        self.active = False

    def sample(self) -> float:
        now = self.elapsed()
        delta = now - self.last
        if self.active and 0 <= delta <= self.max_gap:
            self.seconds += delta
        self.last = now
        return self.seconds

    def observe(self, run: RunSession, elapsed: float) -> None:
        self.sample()
        self.active = (
            run.state is RunState.ACTIVE
            and run.activity is ActivityState.OPEN
            and run.autonomy == "autonomous"
            and not run.gm_paused
        )
