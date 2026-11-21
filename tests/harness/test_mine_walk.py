"""mine_cells walks to what it cannot reach: the mod's miner breaks only from where the body stands
(2026-11-21: 27 scaffold planks and 28 cells at y 72-75 stayed on a 10-high lighthouse)."""

import json
import math
import sys
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402


class ReachWorld:
    def __init__(self, blocks, pos):
        self.blocks, self.pos, self.walks = dict(blocks), pos, []

    def call(self, name, args):
        if name == "getGameState":
            return json.dumps({"inGame": True, "self": {"pos": "%.1f,%.1f,%.1f" % self.pos}}), False
        if name == "getBlockAt":
            return json.dumps({"id": self.blocks.get((args["x"], args["y"], args["z"]), "minecraft:air")}), False
        if name == "scanBox":
            cells, palette = [], []
            for (x, y, z), b in sorted(self.blocks.items()):
                if args["x1"] <= x <= args["x2"] and args["y1"] <= y <= args["y2"] and args["z1"] <= z <= args["z2"]:
                    if b not in palette:
                        palette.append(b)
                    cells += [x, y, z, palette.index(b)]
            return json.dumps({"ok": True, "palette": palette, "cells": cells, "unloaded": 0}), False
        if name == "mineBlocks":
            eye = (self.pos[0], self.pos[1] + 1.62, self.pos[2])
            for x, y, z in json.loads(args["blocks"]):
                if math.dist(eye, (x + 0.5, y + 0.5, z + 0.5)) <= 4.5:
                    self.blocks.pop((x, y, z), None)
            return json.dumps({"ok": True}), False
        if name in ("mineStatus",):
            return json.dumps({"mining": False, "remaining": 0}), False
        if name == "ExecuteCommand":
            return json.dumps({"ok": True}), False
        raise AssertionError(name)


def ground(blocks, x0, x1, z0, z1, y):
    for x in range(x0, x1 + 1):
        for z in range(z0, z1 + 1):
            blocks[(x, y, z)] = "minecraft:grass_block"


def test_a_scaffold_out_of_reach_is_walked_up_to(monkeypatch):
    blocks = {}
    ground(blocks, -6, 12, -6, 6, 64)
    # a tower to stand on: a 3x3 block of stone up to y 70, its top at feet level 71
    for x in range(6, 9):
        for z in range(-1, 2):
            for y in range(65, 71):
                blocks[(x, y, z)] = "minecraft:stone"
    scaffold = [(5, y, 0) for y in range(65, 74)]   # a plank column up to y 73 beside it
    for p in scaffold:
        blocks[p] = "minecraft:spruce_planks"
    world = ReachWorld(blocks, (-4.5, 65.0, 0.5))   # far away on the ground
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)

    breaking = []
    real_call = world.call

    def spy(name, args):
        if name == "ExecuteCommand":
            breaking.append(args["cmd"].endswith("true"))
        return real_call(name, args)

    world.call = spy

    def walk(x, y, z, timeout=60):
        assert breaking and breaking[-1] is False, "walking with breaking on digs through the build"
        world.walks.append((x, y, z))
        world.pos = (x + 0.5, float(y), z + 0.5)
        return "OK"

    monkeypatch.setattr(build_plan, "walk_close", walk)
    left = build_plan.mine_cells(world, scaffold, 5)
    assert left == []
    assert all(p not in world.blocks for p in scaffold)
    assert world.walks and world.walks[0][1] >= 69   # it went up to the high end first


def test_nothing_to_stand_on_gives_the_cells_back(monkeypatch):
    blocks = {(0, 80, 0): "minecraft:spruce_planks"}   # floating, no floor anywhere
    world = ReachWorld(blocks, (0.5, 64.0, 0.5))
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    monkeypatch.setattr(build_plan, "walk_close", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked")))
    assert build_plan.mine_cells(world, [(0, 80, 0)], 5) == [(0, 80, 0)]


class SealWorld(ReachWorld):
    """buildBlocks fills a cell only when it has a face: the cell above must be air at that moment."""

    def call(self, name, args):
        if name == "buildBlocks":
            for x, y, z, b in json.loads(args["blocks"]):
                if (x, y + 1, z) not in self.blocks or (x, y + 1, z) in [tuple(q[:3]) for q in json.loads(args["blocks"])]:
                    self.blocks[(x, y, z)] = b if ":" in b else "minecraft:" + b
            return json.dumps({"ok": True}), False
        return super().call(name, args)


def test_a_cell_sealed_from_above_is_reopened_placed_and_closed(monkeypatch):
    blocks = {(0, 64, 0): "minecraft:stone", (0, 66, 0): "minecraft:white_concrete"}
    world = SealWorld(blocks, (1.5, 65.0, 0.5))
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    monkeypatch.setattr(build_plan, "wait_queue", lambda c, t, **k: {})
    plan = [(0, 65, 0, "minecraft:polished_deepslate"), (0, 66, 0, "minecraft:white_concrete")]
    left = build_plan.reopen_from_above(world, [plan[0]], plan, 5)
    assert left == []
    assert world.blocks[(0, 65, 0)] == "minecraft:polished_deepslate"
    assert world.blocks[(0, 66, 0)] == "minecraft:white_concrete"   # put back


def test_a_missing_cell_with_nothing_of_the_plan_above_is_left(monkeypatch):
    world = SealWorld({(0, 64, 0): "minecraft:stone"}, (1.5, 65.0, 0.5))
    plan = [(0, 65, 0, "minecraft:polished_deepslate")]
    assert build_plan.reopen_from_above(world, plan, plan, 5) == plan
