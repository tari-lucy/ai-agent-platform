"""design_check.py and the new build_plan.py actions: the shape checks find what is wrong with a plan,
the picture is a real PNG, and the cleanup after a build mines the scaffolding and repairs the plan."""

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
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        assert struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0] == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks[kind] = body
        pos += 12 + length
    width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
    raw = zlib.decompress(chunks[b"IDAT"])
    assert len(raw) == height * (1 + width * 3)
    return width, height, raw


def test_the_picture_is_a_valid_png_with_four_distinct_views(tmp_path):
    out = tmp_path / "plan.png"
    width, height = dc.save_views({(x, y, z): "stone_bricks" for x in range(4) for y in range(3) for z in range(6)}, str(out))
    w, h, raw = read_png(out.read_bytes())
    assert (w, h) == (width, height) and w > 100 and h > 100
    colours = {raw[i:i + 3] for i in range(1, len(raw), 3)}
    assert len(colours) > 4  # sky, three shades of the block, edges, the frame


def test_the_four_views_of_an_unsymmetric_block_differ():
    cells = {(x, y, z): "stone" for x in range(5) for y in range(2) for z in range(2)}
    cells[(0, 2, 0)] = "glowstone"
    sheet = dc.render_views(cells)
    width = sheet.w
    half_w, half_h = width // 2, sheet.h // 2
    corners = [bytes(sheet.px[((y * width + x) * 3):((y * width + x) * 3) + 3])
               for y in range(half_h // 2, half_h, 5) for x in range(0, half_w, 7)]
    assert len(set(corners)) > 1


class FakeWorld:
    """A mod that answers what build_plan asks, with a block the builder left behind as scaffolding."""

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
            self.break_on = args["cmd"].endswith("true") if "allowBreak" in args["cmd"] else self.break_on
            return json.dumps({"ok": True}), False
        if name == "cleanupScaffold":
            assert self.break_on, "cleanup needs allowBreak"
            for p in self.scaffold:
                self.cells.pop(p, None)
            self.queue = len(self.scaffold)
            return json.dumps({"ok": True, "queued": self.queue}), False
        if name == "mineStatus":
            left, self.queue = self.queue, 0
            return json.dumps({"mining": False, "remaining": 0 if not left else 0}), False
        if name == "getBlockAt":
            block = self.cells.get((args["x"], args["y"], args["z"]), "minecraft:air")
            return json.dumps({"id": block}), False
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
    world = FakeWorld({(1, 0, 0): "minecraft:stone_bricks"}, scaffold=[(0, 0, 0)])  # a scaffold stood where the plan wants a block
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
    plan.write_text("легенда:\nS = stone\nP = oak_planks\n\nслой 0\nSSS\nSSS\n\nслой 1\nPPP\nPPP\n", encoding="utf-8")
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
