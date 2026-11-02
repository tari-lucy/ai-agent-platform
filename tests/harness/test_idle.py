"""The drive: pushes only when idle in a world, backs off when it leads nowhere."""

import asyncio
from types import SimpleNamespace

from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_idle import IdleSource


class Host:
    def __init__(self):
        self.busy = False

    async def opencode(self, method, path, body=None, *, timeout=60):
        return {"ses_a": {"type": "busy" if self.busy else "idle"}}


def build(in_game=True, **kw):
    clock = {"t": 0.0}
    host = Host()
    game = SimpleNamespace(in_game=in_game)
    runtime = SimpleNamespace(harness=host)
    src = IdleSource(runtime, game, clock=lambda: clock["t"], today=lambda: "d", **kw)
    return src, host, game, clock


def poll(src):
    return asyncio.run(src.poll(None, lambda: None))


def advance(src, clock, seconds, step=15):
    got = []
    for _ in range(int(seconds // step)):
        clock["t"] += step
        got += poll(src)
    return got


def test_a_push_comes_after_the_idle_delay_and_only_in_a_world():
    src, host, game, clock = build(base=180)
    assert advance(src, clock, 150) == []
    [push] = advance(src, clock, 60)
    assert push.source == "idle" and push.level == Level.HIGH and "tour.py" in push.text
    game.in_game = False
    clock["t"] += 3000
    assert poll(src) == []  # not in a world: nothing to push about


def test_a_busy_session_is_never_pushed_and_a_one_line_answer_doubles_the_pause():
    src, host, game, clock = build(base=60, cap=480)
    host.busy = True
    assert advance(src, clock, 600) == []  # she is working on her own
    host.busy = False
    advance(src, clock, 30)
    assert len(advance(src, clock, 60)) == 1  # idle 60+ s: the push
    host.busy = True  # the push starts a run ...
    advance(src, clock, 30)
    host.busy = False  # ... which ends after 30 s: just words
    advance(src, clock, 30)
    assert src.delay == 120  # doubled
    assert advance(src, clock, 60) == []  # the longer pause is respected
    assert len(advance(src, clock, 90)) == 1


def test_a_push_that_starts_real_work_resets_the_pause():
    src, host, game, clock = build(base=60, cap=480)
    src.delay = 240
    advance(src, clock, 300)  # a push goes out
    host.busy = True
    advance(src, clock, 120)  # real work, two minutes
    host.busy = False
    advance(src, clock, 15)
    assert src.delay == 60


def test_the_day_is_capped_and_a_new_day_starts_fresh():
    day = {"d": "d1"}
    src, host, game, clock = build(base=15, per_day=3)
    src.today = lambda: day["d"]
    src.day = "d1"
    pushes = advance(src, clock, 600)
    assert len(pushes) == 3
    day["d"] = "d2"
    assert len(advance(src, clock, 60)) >= 1
