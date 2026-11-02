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


def test_a_player_coming_near_is_reported_once_until_he_leaves():
    src = source(
        [
            (state(players=[("Bob", 90)]), []),
            (state(players=[("Bob", 30)]), []),
            (state(players=[("Bob", 20)]), []),
            (state(players=[]), []),
            (state(players=[("Bob", 10)]), []),
        ]
    )
    poll(src)
    assert kinds(poll(src)) == ["player_near"]
    assert poll(src) == []
    assert poll(src) == []
    assert kinds(poll(src)) == ["player_near"]


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
