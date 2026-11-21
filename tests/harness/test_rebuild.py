"""rebuild: turn a standing building into a new plan. Inside the plan's box the plan is the whole truth, so a block it
does not list there, or a different block, is mined off first; then the plan is built as usual."""

import json
import sys
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402

CONCRETE, GLASS, PLANKS = "minecraft:white_concrete", "minecraft:light_blue_stained_glass", "minecraft:spruce_planks"


class StandingWorld:
    """A world with blocks in it, scanBox, getBlockAt and a mineBlocks that really clears cells."""

    def __init__(self, blocks, unloaded=0):
        self.blocks = dict(blocks)
        self.unloaded = unloaded
        self.mined = []
        self.calls = []

    def call(self, name, args):
        self.calls.append(name)
        if name == "getGameState":   # standing right next to the cells: everything is in reach
            near = min(self.blocks) if self.blocks else (0, 64, 0)
            pos = "%.1f,%.1f,%.1f" % (near[0] + 0.5, near[1], near[2] + 1.5)
            return json.dumps({"inGame": True, "self": {"pos": pos}}), False
        if name == "scanBox":
            cells, palette = [], []
            for (x, y, z), b in sorted(self.blocks.items()):
                if not (args["x1"] <= x <= args["x2"] and args["y1"] <= y <= args["y2"]
                        and args["z1"] <= z <= args["z2"]):
                    continue
                if b not in palette:
                    palette.append(b)
                cells += [x, y, z, palette.index(b)]
            return json.dumps({"ok": True, "palette": palette, "cells": cells, "unloaded": self.unloaded}), False
        if name == "getBlockAt":
            return json.dumps({"id": self.blocks.get((args["x"], args["y"], args["z"]), "minecraft:air")}), False
        if name == "mineBlocks":
            for p in json.loads(args["blocks"]):
                self.mined.append(tuple(p))
                self.blocks.pop(tuple(p), None)
            return json.dumps({"ok": True}), False
        if name == "mineStatus":
            return json.dumps({"mining": False, "remaining": 0}), False
        if name == "ExecuteCommand":
            return json.dumps({"ok": True}), False
        raise AssertionError(name)


NEW_PLAN = [(0, 0, 0, CONCRETE), (1, 0, 0, CONCRETE), (0, 1, 0, GLASS)]


def test_removals_are_extras_and_wrong_material_top_down():
    world = {
        (0, 0, 0): CONCRETE,          # right: stays
        (1, 0, 0): PLANKS,            # wrong material: off, then the plan puts concrete there
        (1, 1, 0): CONCRETE,          # not in the plan: off
        (0, 1, 0): "minecraft:air",   # air is never "removed"
    }
    assert build_plan.rebuild_removals(world, NEW_PLAN) == [(1, 1, 0), (1, 0, 0)]


def test_only_the_box_of_the_stage_is_touched():
    world = {(5, 0, 0): CONCRETE, (1, 3, 0): CONCRETE, (1, 1, 0): CONCRETE}
    # (5,0,0) is outside the plan's box; (1,3,0) above it: neither is the plan's business
    assert build_plan.rebuild_removals(world, NEW_PLAN) == [(1, 1, 0)]
    # a stage of layer 0 only leaves layer 1 alone even when the whole plan is the truth
    assert build_plan.rebuild_removals({(1, 1, 0): CONCRETE}, NEW_PLAN, NEW_PLAN[:2]) == []


def test_names_compare_without_the_namespace():
    plan = [(0, 0, 0, "white_concrete")]
    assert build_plan.rebuild_removals({(0, 0, 0): CONCRETE}, plan) == []


def test_cmd_rebuild_mines_what_must_go_and_then_lets_the_build_run():
    world = StandingWorld({(0, 0, 0): CONCRETE, (1, 0, 0): PLANKS, (1, 1, 0): CONCRETE})
    assert build_plan.cmd_rebuild(world, NEW_PLAN, NEW_PLAN, timeout=5, mode="physical") is None
    assert set(world.mined) == {(1, 1, 0), (1, 0, 0)}
    assert world.blocks == {(0, 0, 0): CONCRETE}
    assert "sendGameCommand" not in world.calls  # no commands in a physical rebuild


def test_cmd_rebuild_refuses_an_unloaded_building():
    world = StandingWorld({(1, 1, 0): CONCRETE}, unloaded=3)
    assert build_plan.cmd_rebuild(world, NEW_PLAN, NEW_PLAN, timeout=5, mode="physical") == 7
    assert world.mined == []


def test_main_rebuild_clears_then_builds(tmp_path, monkeypatch):
    plan = tmp_path / "house.txt"
    plan.write_text("легенда:\n# = white_concrete\nG = light_blue_stained_glass\nслой 0\n##\nслой 1\nG.\n",
                    encoding="utf-8")
    world = StandingWorld({(10, 64, 10): PLANKS, (11, 65, 10): CONCRETE})
    built = []
    monkeypatch.setattr(build_plan, "cmd_build", lambda c, blocks, timeout, whole: built.append(blocks) or 0)
    code = build_plan.main(["rebuild", str(plan), "--origin", "10,64,10", "--ignore-lint"], client=world)
    assert code == 0
    assert set(world.mined) == {(10, 64, 10), (11, 65, 10)}
    assert len(built) == 1 and len(built[0]) == 3
