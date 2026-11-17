"""What her turns on the ChatGPT plan have used: tokens by window, and the last limit.

The plan's remaining allowance is not readable while she works: the provider reports it only when
it refuses a turn. What the platform can show honestly is how much she has used, counted from her
own session (every finished assistant message carries its tokens), and the last limit hit (when it
came, how much the five hours before it had used), so the panel can say «за последние 5 часов
3,1 млн; прошлый раз лимит наступил после 3,4 млн».

Read-only on the session, one small journal on the host's disk, no model call.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

WINDOW = timedelta(hours=5)  # the plan's short window
WEEK = timedelta(days=7)
POLL_EVERY = 60.0  # seconds between two looks at the session
LOOK = 40  # messages read per look
MAX_ROWS = 20_000  # the journal is cut to this when it is read; a week of work is far below it


@dataclass(frozen=True)
class Totals:
    window: int  # tokens in the last five hours
    day: int  # the last 24 hours
    week: int  # the last seven days
    since: datetime | None  # when the counting began: before it, nothing is known
    last_limit_at: datetime | None
    last_limit_tokens: int | None  # used in the five hours before that limit


def _num(value: Any) -> int:
    try:
        return max(0, int(value))
    except TypeError, ValueError:
        return 0


def turn_tokens(info: dict[str, Any]) -> int:
    """New tokens of one finished assistant message: what was sent anew, written and reasoned
    (cache reads are the same context again and are counted apart)."""
    tokens = info.get("tokens") or {}
    return _num(tokens.get("input")) + _num(tokens.get("output")) + _num(tokens.get("reasoning"))


class PlanUsage:
    def __init__(
        self,
        path: Path | None,
        clock: Callable[[], datetime],
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path, self.clock, self.mono = path, clock, mono
        self.turns: dict[str, tuple[datetime, int]] = {}
        self.limits: list[tuple[datetime, datetime, int]] = []  # (when, until, tokens before)
        self._next = 0.0
        self._loaded = False

    # ------------------------------------------------------------ journal

    def _load(self) -> None:
        self._loaded = True
        if self.path is None or not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-MAX_ROWS:]
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row["at"])
            except ValueError, KeyError, TypeError:
                continue
            if row.get("kind") == "turn" and isinstance(row.get("id"), str):
                self.turns[row["id"]] = (at, _num(row.get("tokens")))
            elif row.get("kind") == "limit":
                try:
                    self.limits.append(
                        (at, datetime.fromisoformat(row["until"]), _num(row.get("tokens")))
                    )
                except ValueError, KeyError:
                    continue

    def _append(self, row: dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return  # the figures are a convenience: a full disk must not stop her

    # ------------------------------------------------------------ reading the session

    async def poll(
        self,
        opencode: Callable[..., Awaitable[Any]],
        session: str | None = None,
        *,
        force: bool = False,
    ) -> int:
        """Take the finished turns not seen yet from the session; returns how many were new."""
        if not self._loaded:
            self._load()
        if not force and self.mono() < self._next:
            return 0
        self._next = self.mono() + POLL_EVERY
        if session is None:
            session = await self._latest(opencode)
            if session is None:
                return 0
        rows = await opencode("GET", f"/session/{session}/message?limit={LOOK}")
        if not isinstance(rows, list):
            return 0
        fresh = 0
        for row in rows:
            info = row.get("info") if isinstance(row, dict) else None
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            ident, done = info.get("id"), (info.get("time") or {}).get("completed")
            if not isinstance(ident, str) or not done or ident in self.turns:
                continue
            tokens = turn_tokens(info)
            if not tokens:
                continue
            at = datetime.fromtimestamp(float(done) / 1000, UTC)
            self.turns[ident] = (at, tokens)
            self._append({"kind": "turn", "at": at.isoformat(), "id": ident, "tokens": tokens})
            fresh += 1
        return fresh

    @staticmethod
    async def _latest(opencode: Callable[..., Awaitable[Any]]) -> str | None:
        sessions = await opencode("GET", "/session")
        if not isinstance(sessions, list):
            return None
        usable = [
            s
            for s in sessions
            if isinstance(s, dict)
            and isinstance(s.get("id"), str)
            and not (s.get("time") or {}).get("archived")
        ]
        if not usable:
            return None
        return max(usable, key=lambda s: (s.get("time") or {}).get("updated", 0))["id"]

    # ------------------------------------------------------------ the limit

    def note_limit(self, until: datetime) -> None:
        """A limit hit that ends at `until`: keep what the five hours before it had used (once)."""
        if not self._loaded:
            self._load()
        if any(abs((known - until).total_seconds()) < 3600 for _, known, _ in self.limits):
            return
        now = self.clock()
        used = self._since(now - WINDOW, now)
        self.limits.append((now, until, used))
        self._append(
            {"kind": "limit", "at": now.isoformat(), "until": until.isoformat(), "tokens": used}
        )

    # ------------------------------------------------------------ the figures

    def _since(self, start: datetime, end: datetime) -> int:
        return sum(tokens for at, tokens in self.turns.values() if start <= at <= end)

    def totals(self) -> Totals:
        if not self._loaded:
            self._load()
        now = self.clock()
        first = min((at for at, _ in self.turns.values()), default=None)
        last = max(self.limits, default=None)
        return Totals(
            window=self._since(now - WINDOW, now),
            day=self._since(now - timedelta(days=1), now),
            week=self._since(now - WEEK, now),
            since=first,
            last_limit_at=last[0] if last else None,
            last_limit_tokens=last[2] if last else None,
        )
