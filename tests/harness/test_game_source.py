"""The game source: what changed in the mod's state becomes events; her own words do not."""

import asyncio

from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_game import GameSource, read_token


def state(hp=20, food=20, pos="0.0,64.0,0.0", players=(), in_game=True, dim="overworld"):
    return {
        "inGame": in_game,
        "self": {"hp": hp, "maxHp": 20, "food": food, "pos": pos, "dimension": dim},
        "players": [{"name": n, "distance": str(d), "hp": 20} for n, d in players],
    }


def source(script):
    """script: a list of (state, chat lines) the fake mod answers with, one per poll."""
    step = {"i": 0}

    def call(tool, arguments):
        current = script[min(step["i"], len(script) - 1)]
        if tool == "getRecentChat":
            step["i"] += 1
            return current[1]
        return current[0]

    return GameSource("http://x/mcp", None, ["Опечатка", "opechatka"], call=call)


def poll(src):
    return asyncio.run(src.poll(None, lambda: None))


def kinds(events):
    return [e.kind for e in events]


def test_hurt_low_health_and_recovery_are_reported_once():
    src = source(
        [
            (state(20), []),
            (state(15), []),
            (state(5), []),
            (state(5), []),  # still low: said once
            (state(18), []),  # healed: the low-health flag resets
            (state(4), []),
        ]
    )
    assert poll(src)[0].kind == "joined"
    hurt = poll(src)
    assert kinds(hurt) == ["hurt"] and hurt[0].level == Level.HIGH and "5" in hurt[0].text
    assert kinds(poll(src)) == ["hurt", "low_health"]
    assert poll(src) == []
    assert poll(src) == []
    assert "low_health" in kinds(poll(src))


def test_a_jump_in_place_with_full_health_after_low_health_means_she_died():
    src = source([(state(20), []), (state(3), []), (state(20, pos="500.0,70.0,500.0"), [])])
    poll(src)
    poll(src)
    events = poll(src)
    assert (
        "died" in kinds(events)
        and [e for e in events if e.kind == "died"][0].level == Level.CRITICAL
    )


def test_a_player_coming_near_is_news_once_and_not_an_alarm():
    src = source(
        [
            (state(players=[("Bob", 90)]), []),
            (state(players=[("Bob", 30)]), []),
            (state(players=[("Bob", 12)]), []),
            (state(players=[("Bob", 8)]), []),
            (state(players=[]), []),
            (state(players=[("Bob", 5)]), []),
        ]
    )
    poll(src)
    assert poll(src) == []  # 30 blocks is not close
    [came] = poll(src)
    assert came.kind == "player_near" and came.level == Level.NORMAL  # news, never CRITICAL
    assert poll(src) == []
    assert poll(src) == []
    assert poll(src) == []  # he left and came back: the same name waits ten minutes


def test_a_crowd_coming_near_is_one_line_and_a_name_repeats_only_after_ten_minutes():
    now = {"t": 0.0}
    crowd = [("A", 3), ("B", 5), ("C", 7)]
    present = {"players": crowd}

    def call(tool, arguments):
        if tool == "getRecentChat":
            return []
        return state(players=present["players"])

    src = GameSource("http://x/mcp", None, ["x"], call=call, clock=lambda: now["t"])
    first = [e for e in poll(src) if e.kind == "player_near"]
    assert len(first) == 1 and "3 players came near" in first[0].text
    present["players"] = []
    poll(src)  # they leave
    present["players"] = crowd
    now["t"] = 100
    assert [e for e in poll(src) if e.kind == "player_near"] == []  # too soon
    present["players"] = []
    poll(src)
    present["players"] = crowd
    now["t"] = 700
    assert len([e for e in poll(src) if e.kind == "player_near"]) == 1  # ten minutes passed


def test_chat_mentions_wake_but_her_own_lines_and_the_first_look_do_not():
    old = ["<Bob> hi all"]
    src = source(
        [
            (state(), old),
            (state(), old + ["<Bob> hey Опечатка, come here", "<Опечатка> sure, coming"]),
            (
                state(),
                old
                + [
                    "<Bob> hey Опечатка, come here",
                    "<Опечатка> sure, coming",
                    "<Eve> nice weather",
                    "<Eve> yes",
                ],
            ),
        ]
    )
    first = poll(src)
    assert "chat" not in kinds(first)  # the old lines are only remembered
    second = poll(src)
    assert kinds(second) == ["mention"] and second[0].level == Level.HIGH
    third = poll(src)
    assert kinds(third) == ["chat"] and third[0].level == Level.LOW and third[0].count == 2


def test_an_unreachable_mod_is_silence_and_focus_follows_the_world():
    def broken(tool, arguments):
        raise OSError("tunnel off")

    src = GameSource("http://x/mcp", None, ["x"], call=broken)
    assert poll(src) == [] and not src.reachable and src.focus() == "free"
    src2 = source([(state(), [])])
    poll(src2)
    assert src2.focus() == "game"
    left = source([(state(), []), (state(in_game=False), [])])
    poll(left)
    assert kinds(poll(left)) == ["left"] and left.focus() == "free"


def test_the_token_file_must_be_a_small_plain_file(tmp_path):
    good = tmp_path / "t.txt"
    good.write_text("secret-value\n", encoding="utf-8")
    assert read_token(good) == "secret-value"
    link = tmp_path / "link.txt"
    link.symlink_to(good)
    assert read_token(link) is None
    big = tmp_path / "big.txt"
    big.write_text("x" * 1000, encoding="utf-8")
    assert read_token(big) is None
    assert read_token(tmp_path / "missing.txt") is None


def test_the_token_path_must_stay_inside_her_folder(tmp_path):
    inside = tmp_path / "her"
    (inside / "projects").mkdir(parents=True)
    (inside / "projects" / "token.txt").write_text("ok-token", encoding="utf-8")
    assert read_token(inside / "projects" / "token.txt", inside) == "ok-token"
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "token.txt").write_text("host-secret", encoding="utf-8")
    (inside / "linked").symlink_to(outside)  # a directory link made from her container
    assert read_token(inside / "linked" / "token.txt", inside) is None


def mob_source(rows_per_poll, states):
    step = {"i": -1}

    def call(tool, arguments):
        if tool == "getGameState":
            step["i"] += 1
        i = min(step["i"], len(states) - 1)
        if tool == "getRecentChat":
            return []
        if tool == "getEntitiesInfo":
            if rows_per_poll is None:
                return ""  # an older mod: the tool is unknown
            return rows_per_poll[min(i, len(rows_per_poll) - 1)]
        return states[i]

    return GameSource("http://x/mcp", None, ["Опечатка"], call=call)


def mob(kind, distance, hostile=True):
    return {"type": kind, "distance": str(distance), "is_hostile": str(hostile).lower()}


def test_a_hostile_mob_coming_near_is_reported_and_named_in_the_hurt_event():
    src = mob_source(
        [[mob("zombie", 30)], [mob("zombie", 5), mob("cow", 3, False)], [mob("zombie", 2)]],
        [state(20), state(20), state(14)],
    )
    poll(src)
    near = poll(src)
    assert kinds(near) == ["hostile_near"] and "zombie" in near[0].text
    hurt = poll(src)
    assert kinds(hurt) == ["hurt"] and "zombie 2m" in hurt[0].context


def test_a_mod_without_the_entity_tool_is_not_asked_over_and_over():
    calls = []
    inner = mob_source(None, [state(20)])

    def counting(tool, arguments):
        calls.append(tool)
        return inner._call(tool, arguments)

    src = GameSource("http://x/mcp", None, ["x"], call=counting)
    for _ in range(5):
        poll(src)
    assert calls.count("getEntitiesInfo") == 1
