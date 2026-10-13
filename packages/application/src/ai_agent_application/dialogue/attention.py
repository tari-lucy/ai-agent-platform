"""Attention: a finite, deterministic filter between events and the (expensive) model call.

No model is asked about a message that attention does not select. Repeats merge, stale signals
expire, a flood of viewer chatter has its own budget so it cannot crowd out the main activity, and
a new signal does not displace the current goal unless it outscores that goal by the switching
cost. The score is a sum of explicit parts; the parts are returned as reason codes for the panel.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .model import AttentionStats, Goal, Signal, SignalKind

DEFAULT_TTL: dict[SignalKind, float] = {
    "operator_message": 3600.0,
    "viewer_message": 120.0,
    "obligation_due": 900.0,
}
BASE_SCORE: dict[SignalKind, float] = {
    "operator_message": 100.0,
    "obligation_due": 70.0,
    "viewer_message": 20.0,
}


@dataclass(frozen=True)
class AttentionPolicy:
    capacity: int = 64
    viewer_budget: int = 6  # undirected viewer signals accepted per window
    viewer_window: float = 60.0
    directed_bonus: float = 35.0
    overdue_bonus: float = 15.0
    wait_bonus_per_second: float = 0.1
    wait_bonus_cap: float = 20.0
    switch_cost: float = 25.0
    recent_penalty: float = 30.0
    recent_window: float = 120.0


@dataclass
class _Entry:
    signal: Signal
    arrived: datetime
    sequence: int
    merged: int = 1


@dataclass(frozen=True)
class Selection:
    signal: Signal
    score: float
    merged: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Offer:
    result: str  # accepted | coalesced | dropped_budget | dropped_full
    pending: int


@dataclass
class AttentionQueue:
    policy: AttentionPolicy = field(default_factory=AttentionPolicy)
    stats: AttentionStats = field(default_factory=AttentionStats)
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _viewer_times: deque[datetime] = field(default_factory=deque)
    _recent: deque[tuple[datetime, str]] = field(default_factory=deque)
    _sequence: int = 0

    # ---- intake -----------------------------------------------------------------------

    def offer(self, signal: Signal, now: datetime) -> Offer:
        self._expire(now)
        existing = self._entries.get(signal.key)
        if existing is not None:
            existing.merged += 1
            self.stats.coalesced += 1
            return Offer("coalesced", len(self._entries))
        if signal.kind == "viewer_message" and not signal.directed:
            window = timedelta(seconds=self.policy.viewer_window)
            while self._viewer_times and now - self._viewer_times[0] >= window:
                self._viewer_times.popleft()
            if len(self._viewer_times) >= self.policy.viewer_budget:
                self.stats.dropped_budget += 1
                return Offer("dropped_budget", len(self._entries))
            self._viewer_times.append(now)
        if len(self._entries) >= self.policy.capacity and not self._make_room(signal):
            self.stats.dropped_full += 1
            return Offer("dropped_full", len(self._entries))
        self._sequence += 1
        self._entries[signal.key] = _Entry(signal, now, self._sequence)
        self.stats.accepted += 1
        return Offer("accepted", len(self._entries))

    def _make_room(self, incoming: Signal) -> bool:
        """Evict the weakest entry only if the newcomer is stronger; otherwise drop the newcomer."""
        base = BASE_SCORE[incoming.kind] + (self.policy.directed_bonus if incoming.directed else 0)
        weakest = min(
            self._entries.values(),
            key=lambda e: (self._static(e.signal), -e.sequence),
        )
        if self._static(weakest.signal) >= base:
            return False
        del self._entries[weakest.signal.key]
        return True

    def _static(self, signal: Signal) -> float:
        return BASE_SCORE[signal.kind] + (self.policy.directed_bonus if signal.directed else 0)

    def _expire(self, now: datetime) -> None:
        for key, entry in list(self._entries.items()):
            ttl = entry.signal.ttl_seconds or DEFAULT_TTL[entry.signal.kind]
            if now - entry.arrived >= timedelta(seconds=ttl):
                del self._entries[key]
                self.stats.expired += 1

    # ---- selection --------------------------------------------------------------------

    def note_handled(self, signal: Signal, now: datetime) -> None:
        """Remember what was just acted on, so near-identical triggers do not repeat at once."""
        self._recent.append((now, signal.person_id or signal.account_id or signal.key))
        while len(self._recent) > 64:
            self._recent.popleft()

    def _score(
        self, entry: _Entry, now: datetime, goal: Goal | None
    ) -> tuple[float, tuple[str, ...]]:
        signal, policy = entry.signal, self.policy
        reasons = [signal.kind.upper()]
        score = BASE_SCORE[signal.kind]
        if signal.directed:
            score += policy.directed_bonus
            reasons.append("DIRECTED")
        if signal.kind == "obligation_due":
            reasons.append("COMMITMENT")
        waited = (now - entry.arrived).total_seconds()
        bonus = min(policy.wait_bonus_cap, max(0.0, waited) * policy.wait_bonus_per_second)
        if bonus > 0:
            score += bonus
            reasons.append("WAITING")
        who = signal.person_id or signal.account_id or signal.key
        window = timedelta(seconds=policy.recent_window)
        if any(now - at < window and subject == who for at, subject in self._recent):
            if signal.kind != "operator_message":
                score -= policy.recent_penalty
                reasons.append("RECENT_SIMILAR")
        if goal is not None and not self._continues(goal, signal):
            score -= policy.switch_cost
            reasons.append("SWITCH_COST")
        return score, tuple(reasons)

    @staticmethod
    def _continues(goal: Goal, signal: Signal) -> bool:
        return goal.person_id is not None and goal.person_id == signal.person_id

    def select(self, now: datetime, goal: Goal | None = None) -> Selection | None:
        """Best pending signal that clears the current goal; the rest stays queued (or expires)."""
        self._expire(now)
        best: tuple[float, int, _Entry, tuple[str, ...]] | None = None
        for entry in self._entries.values():
            score, reasons = self._score(entry, now, goal)
            if (
                goal is not None
                and entry.signal.kind != "operator_message"
                and score <= goal.priority
            ):
                continue  # the current goal stays; this signal waits (or ages out)
            candidate = (score, -entry.sequence, entry, reasons)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None:
            if goal is not None and self._entries:
                self.stats.deferred += 1
            return None
        score, _, entry, reasons = best
        del self._entries[entry.signal.key]
        self.stats.selected += 1
        for reason in reasons:
            self.stats.reasons[reason] = self.stats.reasons.get(reason, 0) + 1
        return Selection(entry.signal, score, entry.merged, reasons)

    def clear(self) -> None:
        """Forget everything queued (STOP): a restarted session does not replay old signals."""
        self._entries.clear()

    def pending(self) -> int:
        return len(self._entries)

    def snapshot(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            counts[entry.signal.kind] = counts.get(entry.signal.kind, 0) + 1
        return counts
