"""Virtual time for scenarios: nothing here reads the wall clock or sleeps for real."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

START = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


class VirtualClock:
    """Deterministic clock: ``advance`` is the only way time moves; callbacks run in order."""

    def __init__(self, start: datetime = START) -> None:
        if start.utcoffset() != timedelta(0):
            raise ValueError("UTC_REQUIRED")
        self.start, self.now = start, start
        self._scheduled: list[tuple[datetime, int, Callable[[], Awaitable[None]]]] = []
        self._order = 0

    def __call__(self) -> datetime:
        return self.now

    def at(self, moment: datetime, action: Callable[[], Awaitable[None]]) -> None:
        self._order += 1
        self._scheduled.append((moment, self._order, action))

    async def advance(self, **delta: float) -> datetime:
        """Move forward; due callbacks fire at their own moment, not at the end of the jump."""
        target = self.now + timedelta(**delta)
        if target < self.now:
            raise ValueError("TIME_BACKWARDS")
        while True:
            due = sorted(
                (item for item in self._scheduled if item[0] <= target), key=lambda i: (i[0], i[1])
            )
            if not due:
                break
            moment, order, action = due[0]
            self._scheduled.remove((moment, order, action))
            self.now = max(self.now, moment)
            await action()
        self.now = target
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Drop-in for ``asyncio.sleep`` in retry loops: virtual time advances, nothing waits."""
        await self.advance(seconds=max(0.0, seconds))
