"""The reflexes: the operator's rules for fights, flight and death, without a model."""

import asyncio

from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_game import GameSource
from ai_agent_core.m3_composition.harness_stance import Action, Hostile, Reflex, Stance

ME = (0.0, 64.0, 0.0)


def zombie(distance=4.0, pos=(4.0, 64.0, 0.0)):
    return Hostile("zombie", distance, pos)


def kinds(actions):
    return [a.kind for a in actions]


def test_mobs_are_fought_above_the_threshold_and_fled_at_or_below_it():
    r = Reflex()
    assert kinds(r.decide(0, 15, ME, [zombie()], True, [], "")) == ["stop", "fight_mobs"]
    assert r.decide(1, 15, ME, [zombie()], False, [], "") == []  # already fighting: no repeats
    run = r.decide(20, 10, ME, [zombie()], True, [], "")  # 10 is not above 10: run
    assert kinds(run) == ["stop", "flee_to"]
    x, y, z = run[1].point
    assert x < 0 and y == 64 and abs(x) > 20  # straight away from a zombie standing at +x


def test_a_far_hostile_is_not_a_reason_and_the_fight_ends_when_it_is_calm():
    r = Reflex()
    assert r.decide(0, 20, ME, [zombie(12.0)], False, [], "") == []
    r.decide(1, 20, ME, [zombie()], False, [], "")
    assert r.mode == "fighting_mobs"
    assert r.decide(30, 20, ME, [], False, [], "") == []  # calm once
    assert kinds(r.decide(31, 20, ME, [], False, [], "")) == ["stop"]  # calm twice: stop
    assert r.mode == "idle"


def test_a_player_who_hit_her_is_hit_back_but_only_with_evidence():
    r = Reflex()
    near = [("Bob", 3.0)]
    # No evidence of who hit her: nothing, even with a player right there.
    assert r.decide(0, 18, ME, [], True, near, "") == []
    act = r.decide(1, 18, ME, [], True, near, "Bob hit you for 4")
    assert kinds(act) == ["stop", "punk"] and act[1].target == "Bob"
    low = Reflex()
    act = low.decide(0, 8, ME, [], True, near, "bob attacked you")
    assert kinds(act) == ["stop", "run_away"] and act[1].target == "Bob" and act[1].distance == 25
    never = Reflex(Stance(pvp="never"))
    assert never.decide(0, 18, ME, [], True, near, "Bob hit you") == []
    far = Reflex()
    assert far.decide(0, 18, ME, [], True, [("Bob", 30.0)], "Bob hit you") == []


class Mod:
    """A fake mod that records every tool call and answers from a script."""

    def __init__(self, hp=20, hostile=True, in_game=True):
        self.calls, self.hp, self.hostile, self.in_game = [], hp, hostile, in_game

    def __call__(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "getGameState":
            return {
                "inGame": self.in_game,
                "self": {"hp": self.hp, "maxHp": 20, "food": 20, "pos": "0.0,64.0,0.0",
                         "dimension": "overworld"},
                "players": [],
            }  # fmt: skip
        if tool == "getEntitiesInfo":
            if not self.hostile:
                return []
            return [{"type": "zombie", "distance": "4.0", "position": "4, 64, 0",
                     "is_hostile": "true"}]  # fmt: skip
        if tool == "getRecentChat":
            return []
        return ""


def source(mod, clock=None):
    return GameSource("http://x/mcp", None, ["x"], call=mod, reflex=Reflex(), clock=clock)


def poll(src):
    return asyncio.run(src.poll(None, lambda: None))


def test_the_game_source_sends_the_fight_command_and_stop_ends_it():
    mod = Mod(hp=20)
    src = source(mod)
    poll(src)
    sent = [(t, a) for t, a in mod.calls if t in ("ExecuteCommand", "stopPathing")]
    assert ("ExecuteCommand", {"cmd": "@hero"}) in sent
    mod.calls.clear()
    asyncio.run(src.close())  # STOP: whatever she started is stopped
    assert [t for t, _ in mod.calls] == ["stopPathing", "punkStop", "runAwayStop"]
    mod.calls.clear()
    asyncio.run(src.close())
    assert mod.calls == []  # nothing to stop the second time


def test_a_death_without_a_respawn_for_a_minute_asks_the_operator_once():
    now = {"t": 0.0}
    mod = Mod(hp=3, hostile=False)
    src = source(mod, clock=lambda: now["t"])
    poll(src)  # low health: she is "dying"
    mod.in_game = False
    poll(src)  # gone from the world
    now["t"] = 30
    assert poll(src) == []  # a respawn takes seconds; half a minute is still quiet
    now["t"] = 61
    [event] = poll(src)
    assert event.kind == "stuck_dead" and event.level == Level.CRITICAL
    assert "ticket_raise" in event.text
    now["t"] = 200
    assert poll(src) == []  # said once


def test_a_normal_respawn_says_nothing():
    now = {"t": 0.0}
    mod = Mod(hp=3, hostile=False)
    src = source(mod, clock=lambda: now["t"])
    poll(src)
    mod.in_game = False
    poll(src)
    now["t"] = 5
    mod.in_game, mod.hp = True, 20
    poll(src)
    now["t"] = 100
    assert [e for e in poll(src) if e.kind == "stuck_dead"] == []


def test_the_stop_action_is_a_plain_value():
    assert Action("stop").kind == "stop" and Action("stop").point is None


def walker(positions, busy=True):
    """A fake mod that answers pathStatus with a scripted position at each poll."""
    step = {"i": -1}
    calls = []

    def call(tool, arguments):
        calls.append(tool)
        if tool == "getGameState":
            step["i"] += 1
            return {
                "inGame": True,
                "self": {"hp": 20, "maxHp": 20, "food": 20, "pos": "0.0,64.0,0.0",
                         "dimension": "overworld"},
                "players": [],
            }  # fmt: skip
        if tool == "pathStatus":
            x = positions[min(step["i"], len(positions) - 1)]
            return {"busy": busy, "pos": f"{x},64.0,0.0", "goal": "10,90,0"}
        return [] if tool in ("getRecentChat", "getEntitiesInfo") else ""

    return call, calls


def test_a_walk_that_goes_nowhere_is_stopped_and_she_is_told_once():
    now = {"t": 0.0}
    call, calls = walker([5.0] * 30)
    src = GameSource(
        "http://x/mcp", None, ["x"], call=call, reflex=Reflex(), clock=lambda: now["t"]
    )
    stuck = []
    for second in range(0, 120, 5):
        now["t"] = float(second)
        stuck += [e for e in poll(src) if e.kind == "stuck"]
    assert len(stuck) == 1 and stuck[0].level == Level.HIGH and "10,90,0" in stuck[0].text
    assert "stopPathing" in calls  # the pushing was stopped


def test_a_walk_that_moves_or_is_not_busy_is_left_alone():
    now = {"t": 0.0}
    call, calls = walker([float(i) * 3 for i in range(30)])
    src = GameSource(
        "http://x/mcp", None, ["x"], call=call, reflex=Reflex(), clock=lambda: now["t"]
    )
    found = []
    for second in range(0, 120, 5):
        now["t"] = float(second)
        found += [e for e in poll(src) if e.kind == "stuck"]
    assert found == [] and "stopPathing" not in calls
    call2, calls2 = walker([5.0] * 30, busy=False)
    idle = GameSource(
        "http://x/mcp", None, ["x"], call=call2, reflex=Reflex(), clock=lambda: now["t"]
    )
    for second in range(0, 120, 5):
        now["t"] = float(second)
        assert [e for e in poll(idle) if e.kind == "stuck"] == []
