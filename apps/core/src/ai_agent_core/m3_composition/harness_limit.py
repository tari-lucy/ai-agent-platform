"""The subscription's usage limit as the persona's sleep.

When the ChatGPT plan's window is used up, every further turn fails with a 429
(usage_limit_reached), OpenCode retries it a few times, the turn ends with an error, and the next
wake (a game event, an idle nudge, a timer) starts another turn that fails the same way. Nothing is
gained and a minute is burnt each time. Here the platform reads the limit error from the session
itself and works out when the window opens again (the provider says so: the body and the x-codex-*
headers carry the reset). It keeps the wakes back until then and puts one wake at the reset, so she
picks her work up by herself.

Read-only on the session; no model call, no money. The money gate and STOP stay where they were.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

DEFAULT_SLEEP = timedelta(minutes=30)  # a limit error without a reset time: look again after this
MARGIN = timedelta(seconds=90)  # the window is opened by the provider's clock, not ours
MAX_SLEEP = timedelta(days=8)  # a weekly window is the longest there is
FULL = 99.5  # used-percent from which a window counts as used up
LOOK_EVERY = 60.0  # seconds between two looks at the session while she is awake
HISTORY = 4  # messages read per look: the newest assistant one is what matters


@dataclass(frozen=True)
class Sleep:
    until: datetime
    since: datetime
    source: str  # "headers" | "body" | "default": where the reset time came from


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # not NaN


def _body(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("responseBody")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_limit(error: Any) -> bool:
    """A usage-limit refusal of the plan (not an ordinary busy server: those OpenCode retries
    itself).
    """
    if not isinstance(error, dict) or error.get("name") != "APIError":
        return False
    data = error.get("data") or {}
    if data.get("statusCode") != 429:
        return False
    headers = {str(k).lower(): v for k, v in (data.get("responseHeaders") or {}).items()}
    text = " ".join(str(data.get(k) or "") for k in ("message", "responseBody")).lower()
    used = (_num(headers.get(f"x-codex-{w}-used-percent")) or 0 for w in ("primary", "secondary"))
    markers = ("usage_limit_reached", "usage limit", "insufficient_quota")
    return any(u >= FULL for u in used) or any(k in text for k in markers)


def reset_after(error: dict[str, Any], base: datetime) -> tuple[timedelta | None, str]:
    """How long after `base` the limit lifts, and where that came from. Of several windows the
    ones that are used up decide (the weekly one outlasts the five-hour one), and of those
    the latest.
    """
    data = error.get("data") or {}
    headers = {str(k).lower(): v for k, v in (data.get("responseHeaders") or {}).items()}
    found: list[float] = []
    for window in ("primary", "secondary"):
        used = _num(headers.get(f"x-codex-{window}-used-percent"))
        after = _num(headers.get(f"x-codex-{window}-reset-after-seconds"))
        at = _num(headers.get(f"x-codex-{window}-reset-at"))
        if after is None and at is not None:
            after = at - base.timestamp()
        if after is not None and (used is None or used >= FULL):
            found.append(after)
    if found:
        return timedelta(seconds=max(found)), "headers"
    body = _body(data).get("error")
    body = body if isinstance(body, dict) else {}
    after = _num(body.get("resets_in_seconds"))
    if after is None and _num(body.get("resets_at")) is not None:
        after = float(body["resets_at"]) - base.timestamp()
    if after is None:
        after = _num(headers.get("retry-after"))
    if after is not None:
        return timedelta(seconds=after), "body"
    return None, "default"


def read_limit(rows: Any, now: datetime) -> Sleep | None:
    """The sleep the newest assistant message of the session calls for, or None (it answered
    fine, or the limit it reported is already over).
    """
    if not isinstance(rows, list):
        return None
    newest: dict[str, Any] | None = None
    stamp = -1.0
    for row in rows:
        info = row.get("info") if isinstance(row, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        created = _num((info.get("time") or {}).get("created")) or 0.0
        if created >= stamp:
            newest, stamp = info, created
    if newest is None or not is_limit(newest.get("error")):
        return None
    times = newest.get("time") or {}
    ms = _num(times.get("completed")) or _num(times.get("created"))
    base = datetime.fromtimestamp(ms / 1000, UTC) if ms else now
    wait, source = reset_after(newest["error"], base)
    until = base + (wait if wait is not None else DEFAULT_SLEEP) + MARGIN
    until = min(until, now + MAX_SLEEP)
    return Sleep(until=until, since=base, source=source) if until > now else None


class LimitGate:
    """Is she asleep because the plan ran out? One instance for the life of the host (the
    dispatcher is built afresh every tick), looking at the session at most once a minute
    while she is awake.
    """

    def __init__(
        self,
        opencode: Callable[..., Awaitable[Any]],
        clock: Callable[[], datetime],
        every: float = LOOK_EVERY,
        mono: Callable[[], float] = time.monotonic,
        on_sleep: Callable[[Sleep], None] | None = None,
    ) -> None:
        self.opencode, self.clock, self.every, self.mono = opencode, clock, every, mono
        self.on_sleep = on_sleep  # told about every new sleep (the plan figures keep a note)
        self.sleep: Sleep | None = None
        self.announced: datetime | None = None  # the `until` a wake was already planned for
        self._looked = float("-inf")

    async def check(self, session: str) -> Sleep | None:
        now = self.clock()
        if self.sleep is not None:
            if now < self.sleep.until:
                return self.sleep
            self.sleep, self._looked = None, float("-inf")  # the time is up: look again at once
        if self.mono() - self._looked < self.every:
            return None
        self._looked = self.mono()
        rows = await self.opencode("GET", f"/session/{quote(session)}/message?limit={HISTORY}")
        found = read_limit(rows, now)
        if found is not None and self.on_sleep is not None:
            self.on_sleep(found)
        self.sleep = found
        return self.sleep

    def fresh(self) -> Sleep | None:
        """The sleep, once, when a wake for its end has not been planned yet."""
        if self.sleep is not None and self.announced != self.sleep.until:
            self.announced = self.sleep.until
            return self.sleep
        return None


def describe(sleep: Sleep, now: datetime) -> str:
    """One line for the panel: how long, and until when (UTC, the host's own clock)."""
    left = max(0, int((sleep.until - now).total_seconds()))
    hours, minutes = left // 3600, (left % 3600 + 59) // 60
    if minutes == 60:
        hours, minutes = hours + 1, 0
    span = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    return f"Сплю: лимит подписки исчерпан, проснусь через {span} (в {sleep.until:%H:%M} UTC)"
