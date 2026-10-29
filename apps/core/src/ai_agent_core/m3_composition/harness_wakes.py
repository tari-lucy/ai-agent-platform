"""Owner of the harness's own wakes (doc33 stage 3, D74): the agent's alarms for itself.

The agent sets one with the platform MCP tool ``wake_me``; the autonomy dispatcher reads
only this port. States move ``new -> pending -> done``, each move atomic under one lock and
behind a commit guard (the harness is current), so a STOP between deciding and committing
changes nothing. A wake left ``pending`` after a crash is the dispatcher's to resolve: its
outcome is unknown and it is never posted again blindly. Local data, possibly personal.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from pydantic import RootModel

from .harness_learning import atomic_json

MAX_OPEN = 20
MAX_MINUTES = 7 * 24 * 60
MAX_TEXT = 2000
State = Literal["new", "pending", "done"]


@dataclass(frozen=True)
class DueWake:
    id: str
    native_session_id: str | None  # None: the dispatcher picks the latest active session
    due_at: datetime
    text: str
    state: State = "new"
    created_at: datetime | None = None
    reserved_at: datetime | None = None
    partition: tuple[str, str, str, str] | None = None
    message_id: str | None = None


class HarnessWakes:
    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path, self.clock = path, clock or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()

    def _load(self) -> list[DueWake]:
        if self.path.is_symlink() or (
            self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024
        ):
            raise ValueError("HARNESS_WAKE_STORE_LIMIT")
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        wakes = []
        for row in rows:
            for key in ("due_at", "created_at", "reserved_at"):
                if row.get(key):
                    row[key] = datetime.fromisoformat(row[key])
            if row.get("partition") is not None:
                row["partition"] = tuple(row["partition"])
            wakes.append(DueWake(**row))
        return wakes

    def _save(self, wakes: list[DueWake]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for wake in wakes:
            row = asdict(wake)
            for key in ("due_at", "created_at", "reserved_at"):
                if row[key] is not None:
                    row[key] = row[key].isoformat()
            rows.append(row)
        if len(json.dumps(rows, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        atomic_json(self.path, RootModel[list[dict[str, Any]]](rows))

    async def set(
        self,
        minutes: int,
        text: str,
        native_session_id: str | None = None,
        *,
        scope: Scope,
        guard: Callable[[], bool],
    ) -> DueWake:
        text = text.strip()
        if not 1 <= minutes <= MAX_MINUTES or not text or len(text) > MAX_TEXT:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        async with self.lock:
            wakes = self._load()
            if sum(w.state != "done" and w.partition == scope.partition for w in wakes) >= MAX_OPEN:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            now = self.clock()
            wake = DueWake(
                uuid4().hex, native_session_id, now + timedelta(minutes=minutes), text,
                created_at=now, partition=scope.partition,
            )  # fmt: skip
            # Done wakes older than a week are history nobody reads any more.
            keep = [w for w in wakes if w.state != "done" or w.due_at > now - timedelta(days=7)]
            if len(keep) >= 4096:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            if not guard():
                raise ContractError(ErrorCode.STALE_CONTEXT)
            self._save([*keep, wake])
            return wake

    async def due(self, limit: int = 10, *, scope: Scope) -> list[DueWake]:
        async with self.lock:
            now = self.clock()
            rows = [
                w
                for w in self._load()
                if w.state == "new" and w.due_at <= now and w.partition == scope.partition
            ]
            return sorted(rows, key=lambda w: w.due_at)[:limit]

    async def pending(self, *, scope: Scope) -> list[DueWake]:
        """Reserved but not confirmed: outcome unknown after a crash or a failed post."""
        async with self.lock:
            return [
                w for w in self._load() if w.state == "pending" and w.partition == scope.partition
            ]

    async def recent_count(self, since: datetime, *, scope: Scope) -> int:
        """Wakes reserved since ``since``: the dispatcher's hourly limit."""
        async with self.lock:
            return sum(
                w.partition == scope.partition
                and w.state != "new"
                and w.reserved_at is not None
                and w.reserved_at >= since
                for w in self._load()
            )

    async def _move(
        self,
        identifier: str,
        before: State,
        after: State,
        guard: Callable[[], bool],
        *,
        scope: Scope,
        native_session_id: str | None = None,
        message_id: str | None = None,
    ) -> bool:
        async with self.lock:
            wakes = self._load()
            for index, wake in enumerate(wakes):
                if wake.id == identifier and wake.partition == scope.partition:
                    if wake.state != before or not guard():
                        return False
                    changed = replace(wake, state=after)
                    if after == "pending":
                        changed = replace(
                            changed,
                            reserved_at=self.clock(),
                            native_session_id=native_session_id,
                            message_id=message_id,
                        )
                    wakes[index] = changed
                    self._save(wakes)
                    return True
            return False

    async def reserve(
        self,
        identifier: str,
        guard: Callable[[], bool],
        *,
        scope: Scope,
        native_session_id: str,
        message_id: str,
    ) -> bool:
        """``new -> pending`` only while ``guard()`` holds; False if anyone was first."""
        from .harness_learning import identifier as checked_id

        checked_id(native_session_id)
        checked_id(message_id)
        return await self._move(
            identifier,
            "new",
            "pending",
            guard,
            scope=scope,
            native_session_id=native_session_id,
            message_id=message_id,
        )

    async def delivered(self, identifier: str, guard: Callable[[], bool], *, scope: Scope) -> bool:
        """``pending -> done`` once the dispatcher has confirmed the message exists."""
        return await self._move(identifier, "pending", "done", guard, scope=scope)

    async def get(self, identifier: str, *, scope: Scope) -> DueWake | None:
        async with self.lock:
            return next(
                (w for w in self._load() if w.id == identifier and w.partition == scope.partition),
                None,
            )
