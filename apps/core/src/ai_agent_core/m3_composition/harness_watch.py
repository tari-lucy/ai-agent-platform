"""The watcher (HarnessPart): sources -> event queue -> one wake, without a model.

Each source is polled on its own interval and only reports what it saw. The queue grades the
news by what she is doing (her focus), and the watcher sets one wake through the same owner as
her own alarms (``HarnessWakes``), so every fence of that path stays: the harness is current,
the run is autonomous and open, the day's money is left, STOP changes nothing in between.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

from .harness_events import Event, EventQueue, Focus, Level, Policy, digest

LOG = logging.getLogger(__name__)

COMMENTS_TEXT = (
    "{count} new comment(s) in your channel. Read them with channel_comments and answer what "
    "deserves it with channel_reply, briefly and in your own way. Empty or hostile ones may be "
    "left without an answer."
)


class Source(Protocol):
    name: str
    interval: float  # seconds between polls

    async def poll(self, scope: Scope, current: Callable[[], None]) -> list[Event]: ...

    async def delivered(self, scope: Scope, current: Callable[[], None]) -> None:
        """Her wake with this source's news is set: stop counting it as unseen."""
        ...


class CommentsSource:
    name = "channel"

    def __init__(self, host: Any, interval: float = 150.0) -> None:
        self.host, self.interval = host, interval

    def _comments(self) -> Any:
        channel = getattr(self.host, "channel", None)
        return channel.comments if channel is not None else None

    async def poll(self, scope: Scope, current: Callable[[], None]) -> list[Event]:
        comments = self._comments()
        if comments is None:
            return []
        count, _last = await comments.unseen(scope, current)
        if not count:
            return []
        return [
            Event(
                "channel", "comments", "unseen", COMMENTS_TEXT.format(count=count),
                Level.NORMAL, count=count, total=True,
            )
        ]  # fmt: skip

    async def delivered(self, scope: Scope, current: Callable[[], None]) -> None:
        comments = self._comments()
        if comments is not None:
            await comments.told(scope, current)


LIVE_SOURCES = frozenset({"voice", "chat", "gamechat"})  # news from people talking to her right now


class Watch:
    """HarnessPart. ``focus`` says what she is doing now; the default is free."""

    def __init__(
        self,
        runtime: Any,
        sources: list[Source],
        path: Path | None = None,
        policy: Policy | None = None,
        focus: Callable[[], Focus] | None = None,
        step: float = 5.0,
    ) -> None:
        self.rt, self.sources, self.step = runtime, sources, step
        self.queue = EventQueue(path, policy=policy)
        self.focus = focus or (lambda: "free")
        self.task: asyncio.Task[None] | None = None
        self.next_check = 0.0
        self.next_poll: dict[str, float] = {}
        self.mine: str | None = None  # id of the last wake this watcher set

    def tick(self) -> None:
        host = self.rt.harness
        if host is None or not host.current():
            return
        if self.task is not None:
            if not self.task.done():
                return
            try:
                self.task.result()
            except (OSError, ValueError, TimeoutError, ContractError) as error:
                LOG.warning("Watch step deferred: %s", type(error).__name__)
            except Exception as error:  # a source of another kind must not end the watcher
                LOG.warning("Watch step failed: %s", type(error).__name__)
            self.task = None
        if time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + self.step
        self.task = asyncio.create_task(self._step())

    async def _step(self) -> None:
        host = self.rt.harness
        if host is None or host.wakes is None:
            return
        run = self.rt.supervisor.snapshot
        if not (run.autonomy == "autonomous" and run.activity.value == "OPEN"):
            return
        scope = run.scope
        if scope.memory_mode != "work":
            return
        epoch = run.control_epoch

        def current() -> None:
            if not host.current() or self.rt.supervisor.snapshot.control_epoch != epoch:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        now = time.monotonic()
        for source in self.sources:
            if now < self.next_poll.get(source.name, 0.0):
                continue
            self.next_poll[source.name] = now + source.interval
            try:
                for event in await source.poll(scope, current):
                    self.queue.add(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # one failing source must not silence the others
                LOG.warning("Source %s deferred: %s", source.name, type(error).__name__)
        focus = self.focus()
        batch = self.queue.decide(focus)
        if batch is None:
            return
        remaining = host.usage.remaining()
        if remaining is not None and remaining <= 0:
            return  # the day's money is gone: the news waits
        if not batch.urgent and await host.wakes.open_count(scope=scope):
            return  # an earlier alarm is still waiting: one wake at a time
        if self.mine is not None:
            last = await host.wakes.get(self.mine, scope=scope)
            if last is not None and last.state != "done":
                return  # she has not read the last one yet: new news waits and joins the next
        text = digest(batch, focus, self.queue.policy, self.queue.clock())
        wake = await host.wakes.set(
            0 if batch.urgent else 1,
            text,
            scope=scope,
            guard=lambda: (
                bool(host.current()) and self.rt.supervisor.snapshot.control_epoch == epoch
            ),
            urgent=batch.urgent,
        )
        self.mine = getattr(wake, "id", None)
        if any(event.source in LIVE_SOURCES for event in batch.events):
            lane = getattr(host, "fast_lane", None)  # a person is talking to her: answer fast
            if lane is not None:
                lane()
        self.queue.delivered(batch)
        for source in self.sources:
            if any(event.source == source.name for event in batch.events):
                try:
                    await source.delivered(scope, current)
                except Exception as error:  # the wake is set: a source's own tidy-up cannot undo it
                    LOG.warning(
                        "Source %s delivered hook failed: %s", source.name, type(error).__name__
                    )

    async def close(self) -> None:
        for source in self.sources:
            closer = getattr(source, "close", None)
            if closer is not None:
                try:
                    await closer()
                except OSError, ValueError, TimeoutError:
                    LOG.warning("Source %s did not close cleanly", source.name)
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
