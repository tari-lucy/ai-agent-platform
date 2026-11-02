"""Servers: a new one is new, a known one is found by name or address, the skeleton is safe."""

import asyncio
from types import SimpleNamespace

from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_server import ServerSource, find_profile, slug_of


def game(info, in_game=True):
    return SimpleNamespace(in_game=in_game, fetch=lambda tool, args: info)


def poll(src):
    return asyncio.run(src.poll(None, lambda: None))


def setup(tmp_path):
    root = tmp_path / "her"
    folder = root / "servers"
    folder.mkdir(parents=True)
    return root, folder


def test_the_slug_comes_from_the_address():
    assert slug_of("play.skybars.me:25565") == "skybars-me"
    assert slug_of("mc.Example.org") == "example-org"
    assert slug_of("", "My Server") == "my-server"
    assert slug_of("") == "server"


def test_a_new_server_gets_a_skeleton_and_a_high_event_once(tmp_path):
    root, folder = setup(tmp_path)
    info = {"ok": True, "address": "play.fun.net", "name": "Fun", "brand": "Paper"}
    src = ServerSource(game(info), folder, root)
    [event] = poll(src)
    assert event.kind == "new" and event.level == Level.HIGH and "RECON.md" in event.text
    text = (folder / "fun-net.md").read_text(encoding="utf-8")
    assert text.startswith("# Fun") and "адрес play.fun.net" in text and "бренд: Paper" in text
    assert poll(src) == []  # the same entry is handled once
    # Leaving and entering again: the profile exists now, so it is a known server.
    src.game.in_game = False
    poll(src)
    src.game.in_game = True
    [again] = poll(src)
    assert again.kind == "known" and again.level == Level.NORMAL and "fun-net.md" in again.text


def test_an_existing_profile_is_found_by_its_title_and_learns_the_address(tmp_path):
    root, folder = setup(tmp_path)
    mine = folder / "skybars.md"
    mine.write_text("# SkyBars\nКак узнать: бренд SkyBars.\n", encoding="utf-8")
    info = {"ok": True, "address": "mc.skybars.me", "name": "Сервер", "brand": "SkyBars"}
    [event] = poll(ServerSource(game(info), folder, root))
    assert event.kind == "known" and "skybars.md" in event.text
    assert "mc.skybars.me" in mine.read_text(encoding="utf-8")  # found by address next time
    assert find_profile(folder, "mc.skybars.me", "", "") == mine
    assert not (folder / "skybars-me.md").exists()  # no duplicate skeleton


def test_nothing_without_the_tool_or_in_single_player_or_outside_a_world(tmp_path):
    root, folder = setup(tmp_path)
    assert poll(ServerSource(game(""), folder, root)) == []  # an older mod
    assert poll(ServerSource(game({"ok": True, "singleplayer": True}), folder, root)) == []
    assert (
        poll(ServerSource(game({"ok": True, "address": "a.b"}, in_game=False), folder, root)) == []
    )
    assert list(folder.iterdir()) == []


def test_the_folder_must_stay_inside_her_workspace(tmp_path):
    root, folder = setup(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    bad = ServerSource(game({"ok": True, "address": "x.y", "name": "X"}), outside / "servers", root)
    assert poll(bad) == []
    assert not (outside / "servers").exists()
