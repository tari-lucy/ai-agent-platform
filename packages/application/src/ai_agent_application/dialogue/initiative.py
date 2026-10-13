"""Unsolicited-contact limits for one persona profile (doc22): finite, per person, exact.

At least 30 s between unsolicited addresses, at most 2 per 5 active minutes to one person, and 10
minutes of silence after two unanswered ones. A person's own message is never an initiative and
resets the unanswered count. A do-not-contact preference outranks every cooldown (the caller
passes it in; this class only counts).
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class InitiativePolicy:
    min_gap: timedelta = timedelta(seconds=30)
    window: timedelta = timedelta(minutes=5)
    max_in_window: int = 2
    silence_after_unanswered: int = 2
    silence: timedelta = timedelta(minutes=10)


@dataclass
class _Person:
    times: deque[datetime] = field(default_factory=deque)
    unanswered: int = 0
    silent_until: datetime | None = None


@dataclass
class InitiativeLimiter:
    policy: InitiativePolicy = field(default_factory=InitiativePolicy)
    _people: dict[str, _Person] = field(default_factory=dict)
    _last_any: datetime | None = None

    def allowed(self, person_id: str, now: datetime) -> tuple[bool, str | None]:
        state = self._people.get(person_id)
        if self._last_any is not None and now - self._last_any < self.policy.min_gap:
            return False, "MIN_GAP"
        if state is None:
            return True, None
        if state.silent_until is not None and now < state.silent_until:
            return False, "SILENCE_AFTER_UNANSWERED"
        recent = [t for t in state.times if now - t < self.policy.window]
        if len(recent) >= self.policy.max_in_window:
            return False, "WINDOW_LIMIT"
        return True, None

    def record(self, person_id: str, now: datetime) -> None:
        state = self._people.setdefault(person_id, _Person())
        state.times.append(now)
        while state.times and now - state.times[0] >= self.policy.window:
            state.times.popleft()
        state.unanswered += 1
        if state.unanswered >= self.policy.silence_after_unanswered:
            state.silent_until = now + self.policy.silence
        self._last_any = now

    def answered(self, person_id: str) -> None:
        """The person wrote to us: the unanswered count and the silence period are over."""
        state = self._people.get(person_id)
        if state is not None:
            state.unanswered, state.silent_until = 0, None
