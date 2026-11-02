"""The watcher's queue: levels by focus, ageing, merging, gaps, persistence, the digest."""

from ai_agent_core.m3_composition.harness_events import (
    Event,
    EventQueue,
    Level,
    digest,
)


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


def queue(tmp_path=None, clock=None):
    clock = clock or Clock()
    path = None if tmp_path is None else tmp_path / "events.json"
    return EventQueue(path, clock), clock


def test_game_news_is_urgent_in_the_game_and_comments_wait():
    q, clock = queue()
    q.add(Event("game", "hit", "zombie", "a zombie hit you", Level.HIGH))
    q.add(Event("channel", "comments", "c", "2 new comments", Level.NORMAL))
    batch = q.decide("game")
    # HIGH moved one level up to CRITICAL by the game focus: now, and the comments ride along?
    assert batch is not None and batch.urgent
    assert [e.source for e in batch.events] == ["game"]  # comments are LOW in the game
    q.delivered(batch)
    assert [e.source for e in q.pending()] == ["channel"]
    # Same pair with the channel in focus: comments first, the game news only HIGH.
    q2, _ = queue()
    q2.add(Event("game", "hit", "zombie", "a zombie hit you", Level.HIGH))
    q2.add(Event("channel", "comments", "c", "2 new comments", Level.NORMAL))
    batch = q2.decide("channel")
    assert batch is not None and not batch.urgent
    assert {e.source for e in batch.events} == {"game", "channel"}


def test_critical_base_wakes_at_once_whatever_the_focus_and_gaps():
    q, _ = queue()
    q.add(Event("game", "death", "d", "you died", Level.CRITICAL))
    for focus in ("game", "channel", "work", "free"):
        batch = q.decide(focus)
        assert batch is not None and batch.urgent


def test_normal_news_waits_for_the_gap_and_low_news_only_when_free():
    q, clock = queue()
    q.add(Event("channel", "comments", "c", "1 new comment", Level.NORMAL))
    q.add(Event("time", "diary", "d", "write the diary", Level.LOW))
    first = q.decide("free")
    assert first is not None and [e.source for e in first.events] == ["channel"]
    q.delivered(first)
    clock.now += 60
    assert q.decide("free") is None  # LOW waits for its own long gap
    clock.now += 61 * 60
    batch = q.decide("free")
    assert batch is not None and batch.events[0].source == "time"
    q.delivered(batch)
    q.add(Event("channel", "comments", "c", "another", Level.NORMAL))
    clock.now += 60
    assert q.decide("free") is None  # NORMAL gap (20 min) not over
    clock.now += 20 * 60
    assert q.decide("free") is not None


def test_low_news_does_not_wake_her_in_the_game_but_old_news_rises_to_normal_at_most():
    q, clock = queue()
    q.add(Event("time", "diary", "d", "write the diary", Level.LOW))
    clock.now += 10 * 60
    assert q.decide("game") is None
    clock.now += 2 * 3600  # two hours of waiting: NORMAL at most, never HIGH or CRITICAL
    batch = q.decide("game")
    assert batch is not None and not batch.urgent
    assert q.policy.effective(batch.events[0], "game", clock.now) == Level.NORMAL


def test_a_repeat_is_counted_and_keeps_ageing_and_the_digest_says_so(tmp_path):
    q, clock = queue(tmp_path)
    q.add(Event("game", "hit", "zombie", "a zombie hit you", Level.HIGH))
    clock.now += 5
    q.add(Event("game", "hit", "zombie", "a zombie hit you again", Level.HIGH))
    [event] = q.pending()
    assert event.count == 2 and event.created == 10_000.0 and "again" in event.text
    batch = q.decide("game")
    text = digest(batch, "game", q.policy, clock.now)
    assert "x2" in text and "game/hit" in text and "focus now: game" in text
    # The queue survives a restart; delivered news is gone and the gaps stay.
    again = EventQueue(tmp_path / "events.json", clock)
    assert [e.count for e in again.pending()] == [2]
    again.delivered(batch)
    reread = EventQueue(tmp_path / "events.json", clock)
    assert reread.pending() == [] and reread.last_wake["high"] == clock.now


def test_the_queue_is_bounded_and_drops_the_least_important_first():
    q, _ = queue()
    q.add(Event("game", "death", "d", "died", Level.CRITICAL))
    for number in range(250):
        q.add(Event("time", "note", str(number), "x", Level.LOW))
    assert len(q.pending()) == 200
    assert any(e.kind == "death" for e in q.pending())


def test_the_watcher_sets_an_urgent_wake_at_once_even_when_an_alarm_waits():
    import asyncio
    from types import SimpleNamespace

    from ai_agent_core.m3_composition.harness_watch import Watch

    class Game:
        name = "game"
        interval = 0

        async def poll(self, scope, current):
            return [Event("game", "death", "d", "you died", Level.CRITICAL)]

        async def delivered(self, scope, current):
            pass

    woken = []

    class Wakes:
        async def open_count(self, *, scope):
            return 3

        async def set(self, minutes, text, *, scope, guard, urgent=False):
            woken.append((minutes, urgent, text))

    run = SimpleNamespace(
        autonomy="autonomous",
        activity=SimpleNamespace(value="OPEN"),
        scope=SimpleNamespace(memory_mode="work"),
        control_epoch=1,
    )
    harness = SimpleNamespace(
        wakes=Wakes(), usage=SimpleNamespace(remaining=lambda: 1.0), current=lambda: True
    )
    runtime = SimpleNamespace(harness=harness, supervisor=SimpleNamespace(snapshot=run))
    watch = Watch(runtime, [Game()], focus=lambda: "channel")
    asyncio.run(watch._step())
    assert len(woken) == 1 and woken[0][:2] == (0, True) and "you died" in woken[0][2]
    assert watch.queue.pending() == []
    # A budget that is gone holds even urgent news back; it waits in the queue.
    harness.usage = SimpleNamespace(remaining=lambda: 0.0)
    asyncio.run(watch._step())
    assert len(woken) == 1 and len(watch.queue.pending()) == 1


def test_a_failing_source_does_not_silence_the_others():
    import asyncio
    from types import SimpleNamespace

    from ai_agent_core.m3_composition.harness_watch import Watch

    class Broken:
        name = "channel"
        interval = 0

        async def poll(self, scope, current):
            raise RuntimeError("the proxy is down")  # not an OSError: a Telegram-like error

        async def delivered(self, scope, current):
            pass

    class Game:
        name = "game"
        interval = 0

        async def poll(self, scope, current):
            return [Event("game", "death", "d", "you died", Level.CRITICAL)]

        async def delivered(self, scope, current):
            pass

    woken = []

    class Wakes:
        async def open_count(self, *, scope):
            return 0

        async def set(self, minutes, text, *, scope, guard, urgent=False):
            woken.append(text)

    run = SimpleNamespace(
        autonomy="autonomous",
        activity=SimpleNamespace(value="OPEN"),
        scope=SimpleNamespace(memory_mode="work"),
        control_epoch=1,
    )
    harness = SimpleNamespace(
        wakes=Wakes(), usage=SimpleNamespace(remaining=lambda: 1.0), current=lambda: True
    )
    runtime = SimpleNamespace(harness=harness, supervisor=SimpleNamespace(snapshot=run))
    watch = Watch(runtime, [Broken(), Game()], focus=lambda: "game")
    asyncio.run(watch._step())
    assert len(woken) == 1 and "you died" in woken[0]
