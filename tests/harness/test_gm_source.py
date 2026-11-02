"""The game master source: it says that an offer waits, never what it says."""

import asyncio
from types import SimpleNamespace

from ai_agent_core.m3_composition.harness_events import Event, EventQueue, Level
from ai_agent_core.m3_composition.harness_gm import GmSource


def runtime(enabled=True, autonomy="autonomous", paused=False):
    run = SimpleNamespace(autonomy=autonomy, gm_paused=paused)
    return SimpleNamespace(
        private_api_budget=SimpleNamespace(gm_enabled=enabled),
        supervisor=SimpleNamespace(snapshot=run),
    )


class Fake(GmSource):
    def __init__(self, rt, offers):
        super().__init__(rt)
        self.offers = offers

    async def live_offers(self, scope):
        return list(self.offers)


SCOPE = SimpleNamespace(memory_mode="work")


def poll(src, scope=SCOPE):
    return asyncio.run(src.poll(scope, lambda: None))


def test_a_live_offer_is_reported_once_without_its_content():
    src = Fake(runtime(), ["opp-1"])
    [event] = poll(src)
    assert (event.source, event.kind, event.level) == ("gm", "offer", Level.LOW)
    assert "persona_brief" in event.text and "opp-1" not in event.text
    assert poll(src) == []  # the same offer is not repeated
    src.offers = ["opp-1", "opp-2"]
    assert len(poll(src)) == 1  # a new one is
    src.offers = []
    assert poll(src) == []
    src.offers = ["opp-1"]
    assert len(poll(src)) == 1  # gone and back: new again


def test_nothing_when_the_master_is_off_paused_or_she_is_not_on_her_own():
    for rt in (runtime(enabled=False), runtime(paused=True), runtime(autonomy="observation")):
        assert poll(Fake(rt, ["opp-1"])) == []
    assert poll(Fake(runtime(), ["opp-1"]), SimpleNamespace(memory_mode="test")) == []


def test_an_offer_wakes_her_only_when_she_is_free():
    clock = {"now": 10_000.0}
    q = EventQueue(None, lambda: clock["now"])
    q.add(Event("gm", "offer", "offer", "offer waits", Level.LOW))
    assert q.decide("game") is None and q.decide("work") is None and q.decide("channel") is None
    batch = q.decide("free")  # LOW -> NORMAL when free, and nothing woke her lately
    assert batch is not None and not batch.urgent
