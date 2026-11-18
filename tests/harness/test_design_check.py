"""design_check.py and the new build_plan.py actions.

The shape checks find what is wrong with a plan, the picture is a real PNG, and the cleanup after a
build mines the scaffolding and repairs the plan.
"""

import json
import struct
import sys
import zlib
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402
import design_check as dc  # noqa: E402


def gazebo():
    """The same shape as the first real build: stone rim, oak floor, four posts, a stepped roof."""
    cells = {}
    for x in range(1, 10):
        for z in range(1, 10):
            cells[(x, 0, z)] = "stone_bricks"
    for x in range(2, 9):
        for z in range(2, 9):
            cells[(x, 0, z)] = "oak_planks"
    for x, z in ((1, 1), (1, 9), (9, 1), (9, 9)):
        for y in (1, 2, 3):
            cells[(x, y, z)] = "stripped_spruce_log"
    for inset in range(5):
        for x in range(inset, 11 - inset):
            for z in range(inset, 11 - inset):
                cells[(x, 4 + inset, z)] = "spruce_planks"
    return cells


def texts(findings):
    return [f.text for f in findings]


def test_a_clean_but_heavy_gazebo_gets_notes_not_errors():
    found = dc.lint(gazebo())
    assert not [f for f in found if f.level == "ошибка"]
    assert any("крыша" in t and "тяжело" in t for t in texts(found))
    assert any("видимой сверху" in t for t in texts(found))  # a roof of one material


def test_a_block_in_the_air_is_an_error():
    cells = gazebo()
    cells[(5, 20, 5)] = "stone"
    found = dc.lint(cells)
    assert [f for f in found if f.level == "ошибка" and "висит" in f.text and "(5,20,5)" in f.text]


def test_a_supported_roof_is_not_floating():
    assert dc.floating(gazebo()) == []


def test_almost_symmetric_with_one_odd_block_is_a_note():
    cells = gazebo()
    del cells[(3, 4, 5)]
    assert any("почти симметрично" in t for t in texts(dc.lint(cells)))


def test_a_one_block_hole_in_a_floor_is_found():
    cells = gazebo()
    del cells[(5, 0, 5)]
    assert any("дырка" in t and "(5,0,5)" in t for t in texts(dc.lint(cells)))


def test_walled_in_light_and_no_light_at_all():
    cells = {(x, y, z): "stone" for x in range(3) for y in range(3) for z in range(3)}
    cells[(1, 1, 1)] = "glowstone"
    assert any("замурован" in t for t in texts(dc.lint(cells)))
    big = {(x, y, z): "stone_bricks" for x in range(8) for y in range(4) for z in range(8)}
    assert any("нет ни одного источника света" in t for t in texts(dc.lint(big)))


def test_an_empty_plan_is_an_error():
    assert dc.lint({})[0].level == "ошибка"


def read_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        assert (
            struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
            == zlib.crc32(kind + body) & 0xFFFFFFFF
        )
        chunks[kind] = body
        pos += 12 + length
    width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
    raw = zlib.decompress(chunks[b"IDAT"])
    assert len(raw) == height * (1 + width * 3)
    return width, height, raw


def test_the_picture_is_a_valid_png_with_four_distinct_views(tmp_path):
    out = tmp_path / "plan.png"
    width, height = dc.save_views(
        {(x, y, z): "stone_bricks" for x in range(4) for y in range(3) for z in range(6)}, str(out)
    )
    w, h, raw = read_png(out.read_bytes())
    assert (w, h) == (width, height) and w > 100 and h > 100
    colours = {raw[i : i + 3] for i in range(1, len(raw), 3)}
    assert len(colours) > 4  # sky, three shades of the block, edges, the frame


def test_the_four_views_of_an_unsymmetric_block_differ():
    cells = {(x, y, z): "stone" for x in range(5) for y in range(2) for z in range(2)}
    cells[(0, 2, 0)] = "glowstone"
    sheet = dc.render_views(cells)
    width = sheet.w
    half_w, half_h = width // 2, sheet.h // 2
    corners = [
        bytes(sheet.px[((y * width + x) * 3) : ((y * width + x) * 3) + 3])
        for y in range(half_h // 2, half_h, 5)
        for x in range(0, half_w, 7)
    ]
    assert len(set(corners)) > 1


class FakeWorld:
    """A mod that answers what build_plan asks, with a block left behind as scaffolding."""

    def __init__(self, plan, scaffold):
        self.cells = dict(plan)
        self.scaffold = list(scaffold)
        for p in self.scaffold:
            self.cells[p] = "minecraft:oak_planks"
        self.calls = []
        self.break_on = False
        self.queue = 0

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "getGameState":
            return json.dumps({"inGame": getattr(self, "in_game", True)}), False
        if name == "ExecuteCommand":
            self.break_on = (
                args["cmd"].endswith("true") if "allowBreak" in args["cmd"] else self.break_on
            )
            return json.dumps({"ok": True}), False
        if name == "cleanupScaffold":
            assert self.break_on, "cleanup needs allowBreak"
            for p in self.scaffold:
                self.cells.pop(p, None)
            self.queue = len(self.scaffold)
            self.scaffold = []  # the mod forgets what it has handed to the break queue
            return json.dumps({"ok": True, "queued": self.queue}), False
        if name == "mineStatus":
            left, self.queue = self.queue, 0
            return json.dumps({"mining": False, "remaining": 0 if not left else 0}), False
        if name == "getBlockAt":
            block = self.cells.get((args["x"], args["y"], args["z"]), "minecraft:air")
            return json.dumps({"id": block}), False
        if name == "scanBox" and getattr(self, "scan", True):
            palette, flat = [], []
            for (px, py, pz), block in self.cells.items():
                if (
                    args["x1"] <= px <= args["x2"]
                    and args["y1"] <= py <= args["y2"]
                    and args["z1"] <= pz <= args["z2"]
                ):
                    if block not in palette:
                        palette.append(block)
                    flat += [px, py, pz, palette.index(block)]
            return json.dumps(
                {
                    "ok": True,
                    "palette": palette,
                    "cells": flat,
                    "unloaded": getattr(self, "unloaded", 0),
                }
            ), False
        if name == "mineBlocks":
            for bx, by, bz in json.loads(args["blocks"]):
                self.cells.pop((bx, by, bz), None)
            return json.dumps({"ok": True}), False
        if name == "buildBlocks":
            for x, y, z, b in json.loads(args["blocks"]):
                self.cells[(x, y, z)] = "minecraft:" + b.split(":")[-1]
            return json.dumps({"ok": True}), False
        if name == "buildQueue":
            return json.dumps({"done": True}), False
        raise AssertionError(name)


def test_cleanup_mines_the_scaffolding_turns_break_off_again_and_checks_the_plan(monkeypatch):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    plan = [(x, 0, 0, "minecraft:stone_bricks") for x in range(3)]
    world = FakeWorld({(x, y, z): b for x, y, z, b in plan}, scaffold=[(1, 1, 0), (1, 2, 0)])
    assert build_plan.cmd_cleanup(world, plan, timeout=5) == 0
    assert (1, 1, 0) not in world.cells and (1, 2, 0) not in world.cells
    sets = [a["cmd"] for n, a in world.calls if n == "ExecuteCommand"]
    assert sets == ["@set allowBreak true", "@set allowBreak false"]


def test_cleanup_that_took_a_plan_block_puts_it_back(monkeypatch):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    plan = [(0, 0, 0, "minecraft:oak_planks"), (1, 0, 0, "minecraft:stone_bricks")]
    world = FakeWorld(
        {(1, 0, 0): "minecraft:stone_bricks"}, scaffold=[(0, 0, 0)]
    )  # a scaffold stood where the plan wants a block
    assert build_plan.cmd_cleanup(world, plan, timeout=5) == 0
    assert world.cells[(0, 0, 0)] == "minecraft:oak_planks"


def test_the_lint_action_reads_a_plan_and_stops_on_errors(tmp_path, capsys):
    plan = tmp_path / "p.txt"
    plan.write_text("легенда:\nS = stone\n\nслой 0\nS\n\nслой 3\nS\n", encoding="utf-8")
    assert build_plan.main(["lint", str(plan)]) == 1
    assert "висит" in capsys.readouterr().out


def test_the_picture_action_writes_a_png_into_the_shots_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_plan, "SHOTS", str(tmp_path))
    plan = tmp_path / "gazebo.txt"
    plan.write_text(
        "легенда:\nS = stone\nP = oak_planks\n\nслой 0\nSSS\nSSS\n\nслой 1\nPPP\nPPP\n",
        encoding="utf-8",
    )
    assert build_plan.main(["picture", str(plan)]) == 0
    assert (tmp_path / "gazebo-plan.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "platform_look" in capsys.readouterr().out


def test_a_staged_build_judges_the_form_on_the_whole_plan(tmp_path, capsys):
    plan = tmp_path / "p.txt"
    plan.write_text("легенда:\nS = stone\n\nслой 0\nSS\n\nслой 1\nSS\n", encoding="utf-8")
    # only the second layer is built at this stage, but it is not "floating" within the whole plan
    assert build_plan.main(["check", str(plan), "--origin", "0,0,0", "--layers", "1-1"]) == 0
    assert "висит" not in capsys.readouterr().out


def test_nothing_is_touched_when_the_game_is_not_in_a_world(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "p.txt"
    plan.write_text("легенда:\nS = stone\n\nслой 0\nS\n", encoding="utf-8")
    world = FakeWorld({}, scaffold=[])
    world.in_game = False
    assert build_plan.main(["cleanup", str(plan), "--origin", "0,0,0"], client=world) == 10
    assert [n for n, _ in world.calls] == ["getGameState"]  # no allowBreak, no verify, no rebuild
    assert "не в мире" in capsys.readouterr().out


class Jammed:
    """A builder that stands inside its own footprint until the body is walked out of it."""

    def __init__(self, free_after):
        self.moved = 0
        self.free_after = free_after
        self.placed = 0
        self.calls = []
        self.issued = 0

    def call(self, name, args):
        self.calls.append(name)
        if name == "getGameState":
            return json.dumps({"inGame": True, "self": {"pos": "203.5,71.8,25.6"}}), False
        if name == "getBlockAt":
            solid = args["y"] < 71
            return json.dumps(
                {
                    "id": "minecraft:dirt" if solid else "minecraft:air",
                    "replaceable": "false" if solid else "true",
                }
            ), False
        if name in ("stopPathing", "buildQueueClear"):
            return json.dumps({"ok": True}), False
        if name == "buildBlocks":
            self.issued += 1
            return json.dumps({"ok": True}), False
        if name == "buildQueue":
            if self.moved >= self.free_after:
                return json.dumps({"done": True, "placed": 9}), False
            self.placed = 0
            return json.dumps(
                {"queued": 3, "placed": 0, "blockedByOwnBody": self.issued * 100 + len(self.calls)}
            ), False
        raise AssertionError(name)


def fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(build_plan.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def test_a_build_that_places_nothing_while_blocking_itself_is_reported_stuck(monkeypatch):
    fake_clock(monkeypatch)
    status = build_plan.wait_queue(Jammed(free_after=99), timeout=600)
    assert status.get("stuck") is True


def test_a_build_that_keeps_placing_is_never_stuck(monkeypatch):
    fake_clock(monkeypatch)

    class Working(Jammed):
        def call(self, name, args):
            if name == "buildQueue":
                self.placed += 1
                done = self.placed > 40
                return json.dumps(
                    {
                        "done": done,
                        "queued": 3,
                        "placed": self.placed,
                        "blockedByOwnBody": self.placed * 50,
                    }
                ), False
            return super().call(name, args)

    assert build_plan.wait_queue(Working(free_after=99), timeout=600).get("stuck") is None


def test_a_jammed_stage_walks_out_of_the_footprint_and_is_given_again(monkeypatch):
    fake_clock(monkeypatch)
    world = Jammed(free_after=1)
    walked = []

    def walk(x, y, z, timeout=60):
        walked.append((x, y, z))
        world.moved += 1
        return "OK"

    monkeypatch.setattr(build_plan, "walk_to", walk)
    part = [(x, 1, z, "minecraft:stone") for x in range(210, 214) for z in range(20, 24)]
    status, jammed = build_plan.build_stage(world, part, timeout=600)
    assert jammed is False and status.get("done")
    assert world.issued == 2 and len(walked) == 1
    x, y, z = walked[0]
    assert not (210 <= x <= 213 and 20 <= z <= 23)  # outside the stage's footprint
    assert "stopPathing" in world.calls and "buildQueueClear" in world.calls


def test_a_builder_that_never_frees_up_is_given_up_on_after_three_tries(monkeypatch, capsys):
    fake_clock(monkeypatch)
    world = Jammed(free_after=99)
    monkeypatch.setattr(build_plan, "walk_to", lambda *a, **k: "OK")
    part = [(210, 1, 20, "minecraft:stone")]
    status, jammed = build_plan.build_stage(world, part, timeout=600)
    assert jammed is True and world.issued == build_plan.UNSTICKS + 1
    assert "Строитель встал внутри стройки" in capsys.readouterr().out


def test_a_non_square_plan_turns_without_losing_or_moving_cells():
    cells = {(x, y, z): "stone" for x in range(9) for y in range(2) for z in range(11)}
    cells[(2, 2, 3)] = "glowstone"  # an asymmetric mark to follow
    for turns in range(4):
        turned = dc._rotated(cells, turns)
        assert len(turned) == len(cells)
        xs, _, zs = zip(*turned, strict=True)
        assert (min(xs), min(zs)) == (0, 0)
        assert (max(xs) + 1, max(zs) + 1) == ((9, 11) if turns % 2 == 0 else (11, 9))
    four = dc._rotated(dc._rotated(dc._rotated(dc._rotated(cells, 1), 1), 1), 1)
    assert four == cells  # four quarter turns are the identity
    assert [p for p, n in dc._rotated(cells, 1).items() if n == "glowstone"] == [(3, 2, 6)]


def house_plan():
    return [
        (x, y, z, "minecraft:oak_planks")
        for x in range(0, 3)
        for y in range(0, 2)
        for z in range(0, 3)
    ]


def test_cleanup_finds_scaffolding_in_the_world_not_only_in_the_mods_memory(monkeypatch, capsys):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    plan = house_plan()
    world = FakeWorld({(x, y, z): b for x, y, z, b in plan}, scaffold=[])
    stray = {
        (-1, 0, 1): "minecraft:oak_planks",
        (-1, 1, 1): "minecraft:oak_planks",
        (1, 5, 1): "minecraft:spruce_planks",
    }
    world.cells.update(stray)
    world.cells[(4, 0, 4)] = "minecraft:grass_block"  # nature next to the house stays
    world.cells[(5, 0, 5)] = "minecraft:oak_leaves"
    world.mine_queue = []
    assert build_plan.cmd_cleanup(world, plan, 5, plan) == 0
    assert not any(k in world.cells for k in stray)  # every stray is gone
    assert (
        world.cells[(4, 0, 4)] == "minecraft:grass_block"
        and world.cells[(5, 0, 5)] == "minecraft:oak_leaves"
    )
    assert all(world.cells.get((x, y, z)) == b for x, y, z, b in plan)  # the house is untouched
    assert "Лишнее вокруг постройки: 3 блоков" in capsys.readouterr().out
    assert world.break_on is False


def test_an_earlier_stage_is_not_scaffolding_when_the_whole_plan_is_given(monkeypatch):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    whole = house_plan()
    stage = [b for b in whole if b[1] == 1]  # only the upper layer was just built
    world = FakeWorld({(x, y, z): b for x, y, z, b in whole}, scaffold=[])
    assert build_plan.cmd_cleanup(world, stage, 5, whole) == 0
    assert all(world.cells.get((x, y, z)) == b for x, y, z, b in whole)  # layer 0 still stands


def test_cleanup_says_so_when_the_mod_cannot_scan_and_when_columns_are_not_loaded(
    monkeypatch, capsys
):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    plan = house_plan()
    world = FakeWorld({(x, y, z): b for x, y, z, b in plan}, scaffold=[])
    world.scan = False
    assert build_plan.cmd_cleanup(world, plan, 5, plan) == 0
    assert "без scanBox" in capsys.readouterr().out
    world.scan, world.unloaded = True, 7
    assert build_plan.cmd_cleanup(world, plan, 5, plan) == 0
    assert "Не прогружено 7 столбцов" in capsys.readouterr().out


def test_a_plan_in_logs_does_not_take_the_forest_for_scaffolding():
    names = {"minecraft:oak_log"}
    assert build_plan.is_stray("minecraft:oak_planks", names) is True
    assert (
        build_plan.is_stray("minecraft:oak_log", names) is False
    )  # the plan's own material, but nature-like
    assert build_plan.is_stray("minecraft:grass_block", names) is False
    assert build_plan.is_stray("minecraft:stone_bricks", names) is True
    assert build_plan.is_stray("minecraft:stone", names) is False


def test_a_repair_that_leaves_scaffolding_is_swept_again(monkeypatch):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    plan = house_plan()
    world = FakeWorld({(x, y, z): b for x, y, z, b in plan}, scaffold=[])
    del world.cells[(1, 1, 1)]  # the cleanup took a plan block ...
    original = world.call

    def leaving(name, args):  # ... and placing it again leaves a plank behind
        if name == "buildBlocks":
            world.cells[(-2, 1, 1)] = "minecraft:oak_planks"
        return original(name, args)

    world.call = leaving
    assert build_plan.cmd_cleanup(world, plan, 5, plan) == 0
    assert (-2, 1, 1) not in world.cells and world.cells[(1, 1, 1)] == "minecraft:oak_planks"


def test_cleanup_gives_up_after_the_rounds_and_says_what_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    plan = house_plan()
    world = FakeWorld({(x, y, z): b for x, y, z, b in plan}, scaffold=[])
    original = world.call

    def refusing(name, args):  # the block never stays
        if name == "buildBlocks":
            return json.dumps({"ok": True}), False
        return original(name, args)

    world.call = refusing
    del world.cells[(1, 1, 1)]
    assert build_plan.cmd_cleanup(world, plan, 5, plan) == 5
    assert "НЕ ХВАТАЕТ 1 блоков" in capsys.readouterr().out


class Pillar(Jammed):
    """The pillar that never gets its block: the body jumps on the spot, nothing is placed, nothing is counted."""

    def call(self, name, args):
        if name == "buildQueue":
            return json.dumps(
                {"queued": 5, "placed": 51, "blockedByOwnBody": 69}
            ), False  # not a counter that climbs
        return super().call(name, args)


def test_a_body_that_jumps_on_the_spot_with_a_flat_counter_is_reported_stuck(monkeypatch):
    fake_clock(monkeypatch)
    assert build_plan.wait_queue(Pillar(free_after=99), timeout=600).get("stuck") is True


def test_a_body_that_walks_a_long_way_without_placing_is_not_stuck(monkeypatch):
    fake_clock(monkeypatch)

    class Walking(Pillar):
        steps = 0

        def call(self, name, args):
            if name == "getGameState":
                Walking.steps += 1
                return json.dumps(
                    {"inGame": True, "self": {"pos": f"{Walking.steps * 3.0},66.0,40.0"}}
                ), False
            if name == "buildQueue" and Walking.steps > 60:
                return json.dumps({"done": True, "placed": 52}), False
            return super().call(name, args)

    status = build_plan.wait_queue(Walking(free_after=99), timeout=600)
    assert status.get("stuck") is None and status.get(
        "done"
    )  # it walked the whole time: never "still"


def test_without_a_position_only_the_climbing_counter_counts(monkeypatch):
    fake_clock(monkeypatch)

    class Blind(Pillar):
        def call(self, name, args):
            if name == "getGameState":
                return json.dumps({"inGame": True}), False  # no position in the answer
            return super().call(name, args)

    status = build_plan.wait_queue(Blind(free_after=99), timeout=100)
    assert status.get("stuck") is None  # a flat counter and no position: no reason to call it stuck


def test_a_lantern_behind_glass_is_not_walled_in_but_one_in_stone_is():
    stone = {(x, y, z): "stone" for x in range(3) for y in range(3) for z in range(3)}
    stone[(1, 1, 1)] = "glowstone"
    assert any("замурован" in t for t in texts(dc.lint(stone)))
    glass = {(x, y, z): "glass" for x in range(3) for y in range(3) for z in range(3)}
    glass[(1, 1, 1)] = "glowstone"  # a lamp in a glass room shines
    assert not any("замурован" in t for t in texts(dc.lint(glass)))
    mixed = dict(stone)
    mixed[(1, 1, 2)] = "glass"       # one glass side is enough for the light to get out
    assert not any("замурован" in t for t in texts(dc.lint(mixed)))
