"""terrain.py and the site / level / dig actions of build_plan.py: a site is flat, dry and clear;
levelling fills the low and cuts the high; a dig plan mines cells and refuses a flood."""

import json
import sys
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402
import terrain  # noqa: E402


def heightmap(rows, x1=0, z1=0, top=None, palette=None):
    """rows[z][x] = ground height; `top` overrides (name, y) for single columns (trees, water)."""
    palette = list(palette or ["minecraft:grass_block", "minecraft:oak_leaves", "minecraft:water", "minecraft:lava"])
    depth, width = len(rows), len(rows[0])
    y, ids, gy, gids = [], [], [], []
    for z in range(depth):
        for x in range(width):
            g = rows[z][x]
            gy.append(g)
            gids.append(0)
            name, ty = (top or {}).get((x + x1, z + z1), ("minecraft:grass_block", g))
            y.append(ty)
            ids.append(palette.index(name))
    return {"ok": True, "x1": x1, "z1": z1, "width": width, "depth": depth, "palette": palette,
            "y": y, "id": ids, "gy": gy, "gid": gids}


def maps(rows, **kw):
    return terrain.Heights.from_reply(heightmap(rows, **kw))


def test_the_flattest_window_wins_over_a_slope():
    rows = [[64] * 12 for _ in range(12)]
    for z in range(12):
        for x in range(6, 12):
            rows[z][x] = 64 + (x - 5) * 2          # a slope on the east side
    best = terrain.find_sites(maps(rows), 4, 4, margin=1)[0]
    assert best.span == 0 and best.fill == 0 and best.cut == 0 and best.x + 4 <= 6


def test_water_is_costly_and_lava_is_never_a_site():
    flat = [[64] * 10 for _ in range(10)]
    water = {(x, z): ("minecraft:water", 64) for x in range(10) for z in range(10)}
    lava = {(x, z): ("minecraft:lava", 63) for x in range(10) for z in range(10)}
    assert terrain.find_sites(maps(flat, top=lava), 4, 4, margin=0) == []
    dry = terrain.find_sites(maps(flat), 4, 4, margin=0)[0]
    wet = terrain.find_sites(maps(flat, top=water), 4, 4, margin=0)[0]
    assert wet.water > 0 and wet.score > dry.score


def test_trees_cost_work_and_are_reported():
    flat = [[64] * 8 for _ in range(8)]
    trees = {(x, z): ("minecraft:oak_leaves", 68) for x in range(8) for z in range(2)}
    site = terrain.find_sites(maps(flat, top=trees), 4, 4, margin=0, near=(2, 2))[0]
    assert site.cover == 0 or site.z >= 2            # picks the clear half when there is one


def test_a_window_with_an_unloaded_column_is_not_a_site():
    reply = heightmap([[64] * 6 for _ in range(6)])
    reply["gy"][14] = terrain.UNKNOWN
    h = terrain.Heights.from_reply(reply)
    for site in terrain.find_sites(h, 3, 3, margin=0):
        cols = {(x, z) for x in range(site.x, site.x + 3) for z in range(site.z, site.z + 3)}
        assert (14 % 6, 14 // 6) not in cols


def test_the_best_sites_are_not_shifted_copies_of_each_other():
    found = terrain.find_sites(maps([[64] * 14 for _ in range(14)]), 4, 4, margin=0, top=3)
    assert len(found) == 3
    for i, a in enumerate(found):
        for b in found[i + 1:]:
            assert abs(a.x - b.x) >= 4 or abs(a.z - b.z) >= 4


def test_level_plan_fills_low_columns_and_cuts_high_ones():
    rows = [[64] * 6 for _ in range(6)]
    rows[2][2] = 62       # a hole of two
    rows[3][3] = 67       # a mound of three
    plan = terrain.level_plan(maps(rows), 1, 1, 4, 4, ground=64, margin=0)
    assert sorted((x, y, z) for x, y, z, _ in plan["fill"]) == [(2, 63, 2), (2, 64, 2)]
    assert [(x, y, z) for x, y, z in plan["cut"]] == [(3, 67, 3), (3, 66, 3), (3, 65, 3)]
    assert dict(((x, y, z), b) for x, y, z, b in plan["fill"])[(2, 64, 2)] == "grass_block"  # a cap on top


def test_level_plan_cuts_the_trees_standing_on_the_site():
    rows = [[64] * 4 for _ in range(4)]
    top = {(1, 1): ("minecraft:oak_leaves", 69)}
    plan = terrain.level_plan(maps(rows, top=top), 0, 0, 4, 4, ground=64, margin=0)
    assert [(x, y, z) for x, y, z in plan["cut"]] == [(1, y, 1) for y in (69, 68, 67, 66, 65)]


def test_tiles_are_merged_into_one_map():
    left = maps([[60] * 4 for _ in range(4)], x1=0)
    right = maps([[70] * 4 for _ in range(4)], x1=4)
    merged = terrain.merge([left, right])
    assert merged.width == 8 and merged.ground(1, 1) == 60 and merged.ground(5, 1) == 70
    assert merged.ground_name(5, 1) == "minecraft:grass_block"


class World:
    """A mod with a heightmap, block reads, a break queue and a command line."""

    def __init__(self, rows, blocks=None):
        self.rows, self.calls, self.break_on = rows, [], False
        self.cells = dict(blocks or {})
        self.queue = []

    def height_reply(self, a):
        x1, z1, x2, z2 = a["x1"], a["z1"], a["x2"], a["z2"]
        grid = [[self.rows[z][x] for x in range(x1, x2 + 1)] for z in range(z1, z2 + 1)]
        return heightmap(grid, x1=x1, z1=z1)

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "getGameState":
            return json.dumps({"inGame": True, "self": {"pos": "5.5,65.0,5.5"}}), False
        if name == "getHeightmap":
            return json.dumps(self.height_reply(args)), False
        if name == "getBlockAt":
            return json.dumps({"id": self.cells.get((args["x"], args["y"], args["z"]), "minecraft:air")}), False
        if name == "ExecuteCommand":
            if "allowBreak" in args["cmd"]:
                self.break_on = args["cmd"].endswith("true")
            return json.dumps({"ok": True}), False
        if name == "mineBlocks":
            assert self.break_on
            for x, y, z in json.loads(args["blocks"]):
                self.cells.pop((x, y, z), None)
            return json.dumps({"ok": True}), False
        if name == "mineStatus":
            return json.dumps({"mining": False, "remaining": 0}), False
        if name == "sendGameCommand":
            p = args["command"].split()
            if p[0] == "setblock":
                self.cells[tuple(map(int, p[1:4]))] = p[4]
            else:
                x1, y1, z1, x2, y2, z2 = map(int, p[1:7])
                for x in range(min(x1, x2), max(x1, x2) + 1):
                    for y in range(min(y1, y2), max(y1, y2) + 1):
                        for z in range(min(z1, z2), max(z1, z2) + 1):
                            if p[7].endswith("air"):
                                self.cells.pop((x, y, z), None)
                            else:
                                self.cells[(x, y, z)] = p[7]
            return json.dumps({"ok": True}), False
        raise AssertionError(name)


def test_the_site_action_prints_a_ready_origin(monkeypatch, capsys):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    rows = [[64] * 30 for _ in range(30)]
    assert build_plan.main(["site", "5x6", "--near", "15,15", "--radius", "12"], client=World(rows)) == 0
    out = capsys.readouterr().out
    assert "--origin" in out and "5x6" in out and "перепад 0" in out


def test_the_site_action_says_so_when_there_is_none(monkeypatch, capsys):
    rows = [[64 + (x * 3) for x in range(30)] for _ in range(30)]      # one long steep slope
    assert build_plan.main(["site", "8x8", "--near", "15,15", "--radius", "12"], client=World(rows)) == 7
    assert "нет" in capsys.readouterr().out


def plan_file(tmp_path, text):
    p = tmp_path / "plan.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_level_in_commands_mode_fills_the_hole_and_cuts_the_mound(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    rows = [[64] * 20 for _ in range(20)]
    rows[6][6] = 62
    rows[8][8] = 66
    world = World(rows, {(8, 65, 8): "minecraft:dirt", (8, 66, 8): "minecraft:dirt"})
    plan = plan_file(tmp_path, "легенда:\nS = stone\n\nслой 0\nSSSS\nSSSS\nSSSS\nSSSS\n")
    assert build_plan.main(["level", plan, "--origin", "5,65,5", "--mode", "commands"], client=world) == 0
    assert (8, 65, 8) not in world.cells and (8, 66, 8) not in world.cells          # the mound is cut
    assert world.cells[(6, 63, 6)] == "minecraft:dirt" and world.cells[(6, 64, 6)] == "minecraft:grass_block"
    assert "ГОТОВО" in capsys.readouterr().out


def test_a_plan_that_is_already_level_is_left_alone(tmp_path, capsys):
    rows = [[64] * 20 for _ in range(20)]
    plan = plan_file(tmp_path, "легенда:\nS = stone\n\nслой 0\nSS\nSS\n")
    world = World(rows)
    assert build_plan.main(["level", plan, "--origin", "5,65,5"], client=world) == 0
    assert "Уже ровно" in capsys.readouterr().out
    assert not [n for n, _ in world.calls if n in ("mineBlocks", "sendGameCommand")]


DIG_PLAN = "легенда:\nX = выкопать\n. = пропуск\n\nслой 0\nXXX\nXXX\n\nслой 1\nXXX\nXXX\n"


def test_a_dig_plan_mines_the_cells_top_down_and_turns_break_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    solid = {(x, y, z): "minecraft:stone" for x in range(10, 13) for y in (60, 61) for z in range(5, 7)}
    world = World([[64] * 20 for _ in range(20)], solid)
    assert build_plan.main(["dig", plan_file(tmp_path, DIG_PLAN), "--origin", "10,60,5"], client=world) == 0
    assert not any(k in solid for k in world.cells if k[1] in (60, 61) and 10 <= k[0] <= 12)
    mined = json.loads([a for n, a in world.calls if n == "mineBlocks"][0]["blocks"])
    assert mined[0][1] == 61 and mined[-1][1] == 60            # top layer first
    assert world.break_on is False and "ГОТОВО" in capsys.readouterr().out


def test_a_dig_next_to_water_or_gravel_is_refused(tmp_path, capsys):
    solid = {(x, y, z): "minecraft:stone" for x in range(10, 13) for y in (60, 61) for z in range(5, 7)}
    solid[(11, 62, 5)] = "minecraft:gravel"                   # loose gravel over the roof of the room
    world = World([[64] * 20 for _ in range(20)], solid)
    assert build_plan.main(["dig", plan_file(tmp_path, DIG_PLAN), "--origin", "10,60,5"], client=world) == 12
    assert "ОПАСНО" in capsys.readouterr().out
    assert not [n for n, _ in world.calls if n == "mineBlocks"]


def test_a_dig_cell_never_leaks_into_the_placement_actions(tmp_path, capsys):
    plan = plan_file(tmp_path, "легенда:\nS = stone\nX = выкопать\n\nслой 0\nSX\nSS\n\nслой 1\nSS\n")
    assert build_plan.main(["lint", plan]) == 0
    _, blocks = build_plan.parse(Path(plan).read_text(encoding="utf-8"))
    assert [b for b in blocks if b[3] == build_plan.DIG] == [(1, 0, 0, build_plan.DIG)]
