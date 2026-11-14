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
    assert kinds(second) == ["talk"] and second[0].level == Level.HIGH
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


def chat_source(lines_per_poll, **extra):
    step = {"i": -1}

    def call(tool, arguments):
        if tool == "getGameState":
            step["i"] += 1
            return state()
        if tool == "getRecentChat":
            i = min(step["i"], len(lines_per_poll) - 1)
            return lines_per_poll[i]
        return []

    return GameSource("http://x/mcp", None, ["Опечатка", "opechatka"], call=call, **extra)


def test_a_talk_goes_on_with_the_player_who_addressed_her_and_other_lines_stay_background():
    base = ["<Eve> hi"]
    a = base + ["<Bob> Опечатка, ты тут?"]
    b = a + ["<Bob> а чем занимаешься?", "<Eve> lol"]
    src = chat_source([base, a, b])
    poll(src)
    first = poll(src)
    assert kinds(first) == ["talk"] and first[0].key == "Bob" and first[0].level == Level.HIGH
    second = poll(src)  # Bob follows up without her name: still a talk; Eve is background
    assert sorted(kinds(second)) == ["chat", "talk"]
    assert [e.key for e in second if e.kind == "talk"] == ["Bob"]


def test_the_talk_event_carries_her_notes_about_the_player(tmp_path):
    folder = tmp_path / "her" / "playbook"
    folder.mkdir(parents=True)
    notes = folder / "people.md"
    notes.write_text("Bob: likes parkour\nEve: builds castles\n", encoding="utf-8")
    src = chat_source(
        [["<Eve> hi"], ["<Eve> hi", "Bob » Опечатка, привет"]],
        root=tmp_path / "her",
        people=notes,
    )
    poll(src)
    [event] = poll(src)
    assert event.kind == "talk" and "you know: Bob: likes parkour" in event.context
    assert "castles" not in event.context
    link = folder / "link.md"
    link.symlink_to(notes)
    assert GameSource("u", None, ["x"], people=link, root=tmp_path / "her")._known("Bob") == ""


def test_a_sender_is_read_from_common_chat_formats():
    sender = GameSource._sender
    assert sender("<Bob> hi") == "Bob"
    assert sender("ɢ |   | ☾Креатив☽ [unknown player head] Pavel_9  » го по комнатам") == "Pavel_9"
    assert sender("Eve: hello") == "Eve"
    assert sender("just a server notice") is None


def test_a_fast_voice_takes_the_players_talk_and_the_slow_mind_is_not_woken():
    got: list[tuple[str, str]] = []
    base = ["<Eve> hi"]
    src = chat_source([base, base + ["<Bob> Опечатка, ты тут?", "<Alto Clef> Опечатка done"]])
    src.on_talk = lambda line, known: got.append((line, known))
    poll(src)
    events = poll(src)
    assert [e for e in events if e.kind == "talk"] == []  # nobody is woken by the talk itself
    assert got == [("<Bob> Опечатка, ты тут?", "")]  # the mod's own line is no player's talk


def test_her_own_lines_under_her_bots_nick_are_never_a_talk():
    # the bot's nick is not her name: its lines carry her name ("Я Опечатка") and used to count
    # as somebody talking to her (a loop with herself).
    base = ["<Eve> hi"]
    own = "<LucyTari> TylerDurden, привет! Я Опечатка, журналист."
    src = chat_source([base, base + [own], base + [own, "<Bob> Опечатка, ты тут?"]])
    src.own_nicks.add("lucytari")  # told by her notes
    poll(src)
    assert [e for e in poll(src) if e.kind == "talk"] == []
    third = poll(src)
    assert [e.key for e in third if e.kind == "talk"] == ["Bob"]  # a real player still counts


def test_the_nick_of_her_bot_is_learned_from_what_she_just_sent():
    base = ["<Eve> hi"]
    sent = "Привет, Тайлер. Я Опечатка, кофе в голове кончился."
    src = chat_source(
        [base, base + [f"<LucyTari> {sent}"], base + [f"<LucyTari> {sent}", "<LucyTari> Опечатка, да"]]
    )
    poll(src)
    src.sent(sent)  # the host says what she wrote
    assert [e for e in poll(src) if e.kind == "talk"] == []  # her own line came back
    assert "lucytari" in src.own_nicks  # and her nick is known now
    assert [e for e in poll(src) if e.kind == "talk"] == []  # whatever else that nick writes


def test_she_is_addressed_by_her_name_in_any_case_or_by_the_nick_of_her_bot():
    base = ["<Eve> hi"]
    lines = [
        "<Bob> Ох, Lucytari, заходи скорее в нору!",
        "<Bob> Опечатку видел кто?",
        "<Bob> привет, Опечатке",
        "<Eve> просто болтаю про погоду",
    ]
    src = chat_source([base, base + lines])
    src.own_nicks.add("lucytari")  # the nick of her bot (told by her notes, or learned)
    poll(src)
    talk = [e for e in poll(src) if e.kind == "talk"]
    assert [e.text for e in talk] == lines[:3]  # all three address her; the last one does not


def test_a_server_line_with_her_name_is_not_a_talk():
    base = ["<Eve> hi"]
    src = chat_source([base, base + ["Opechatka присоединился к игре", "[⚡] Опечатка получила награду"]])
    src.own_nicks.add("opechatka")
    poll(src)
    assert [e for e in poll(src) if e.kind == "talk"] == []  # joins and notices are no talk
