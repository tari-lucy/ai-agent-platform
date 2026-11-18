"""stock without /give: in creative mode the stacks come from the creative menu (creativeTakeBlock), which needs
no command rights, so it works on a server where /give is off."""

import json
import sys
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402

BRICKS, GLASS = "minecraft:stone_bricks", "minecraft:glass"


def plan(**counts):
    cells, x = [], 0
    for name, n in counts.items():
        for _ in range(n):
            cells.append((x, 0, 0, "minecraft:" + name))
            x += 1
    return cells


class HotbarWorld:
    """A mod with a real inventory: nine hotbar slots, 27 pack slots, a creative flag and /give on or off."""

    def __init__(self, creative=True, commands=False, new_mod=True):
        self.slots = {i: ("empty", 0) for i in range(36)}
        self.creative, self.commands, self.new_mod = creative, commands, new_mod
        self.calls = []

    def call(self, name, args):
        self.calls.append(name)
        if name == "getHotbar":
            hot = [{"slot": i, "item": self.slots[i][0], "count": self.slots[i][1]} for i in range(9)]
            pack = {}
            for i in range(9, 36):
                if self.slots[i][0] != "empty":
                    pack[self.slots[i][0]] = pack.get(self.slots[i][0], 0) + self.slots[i][1]
            out = {"ok": True, "hotbar": hot, "pack": pack}
            if self.new_mod:
                out["creative"] = self.creative
                out["packEmpty"] = [i for i in range(9, 36) if self.slots[i][0] == "empty"]
            return json.dumps(out), False
        if name == "creativeTakeBlock":
            if not self.new_mod:
                raise AssertionError(name)  # an old mod does not know the tool
            if not self.creative:
                return json.dumps({"ok": False, "error": "not in creative mode"}), False
            slot = args["hotbarSlot"]
            if not 0 <= slot <= 35:
                return json.dumps({"ok": False, "error": "slot must be 0-8 or 9-35"}), False
            count = min(args["count"], 64)
            self.slots[slot] = (args["item"], count)
            return json.dumps({"ok": True, "item": args["item"], "count": count, "hotbarSlot": slot}), False
        if name == "sendGameCommand":
            if self.commands:
                _, _, item, number = args["command"].split()
                free = next(i for i in range(36) if self.slots[i][0] == "empty")
                self.slots[free] = (item, int(number))
            return json.dumps({"ok": True, "note": "sent, not confirmed"}), False  # the mod cannot tell a refusal
        raise AssertionError(name)


def used(world, item):
    return sorted((i, n) for i, (it, n) in world.slots.items() if it == item)


def test_creative_mode_takes_the_stacks_from_the_menu_and_sends_no_command():
    world = HotbarWorld(creative=True, commands=False)
    assert build_plan.cmd_stock(world, plan(stone_bricks=130, glass=20)) == 0
    assert used(world, BRICKS) == [(0, 64), (1, 64), (2, 2)]
    assert used(world, GLASS) == [(3, 20)]
    assert "sendGameCommand" not in world.calls  # a server with commands off is no obstacle


def test_more_stacks_than_the_hotbar_holds_go_to_the_pack_where_the_builder_pulls_from():
    world = HotbarWorld()
    assert build_plan.cmd_stock(world, plan(stone_bricks=64 * 11)) == 0
    assert [slot for slot, _ in used(world, BRICKS)] == list(range(9)) + [9, 10]


def test_a_slot_that_holds_something_is_never_overwritten():
    world = HotbarWorld()
    world.slots[0] = ("minecraft:diamond_pickaxe", 1)
    assert build_plan.cmd_stock(world, plan(stone_bricks=10)) == 0
    assert world.slots[0] == ("minecraft:diamond_pickaxe", 1)
    assert used(world, BRICKS) == [(1, 10)]


def test_what_is_already_in_hand_is_not_taken_again():
    world = HotbarWorld()
    world.slots[4] = (BRICKS, 64)
    assert build_plan.cmd_stock(world, plan(stone_bricks=100)) == 0
    assert used(world, BRICKS) == [(0, 36), (4, 64)]


def test_a_full_inventory_says_so_and_builds_nothing_blind(capsys):
    world = HotbarWorld()
    for i in range(36):
        world.slots[i] = ("minecraft:dirt", 64)
    assert build_plan.cmd_stock(world, plan(stone_bricks=10)) == 6
    assert "нет свободных ячеек" in capsys.readouterr().out


def test_in_survival_it_falls_back_to_give():
    world = HotbarWorld(creative=False, commands=True)
    assert build_plan.cmd_stock(world, plan(stone_bricks=30)) == 0
    assert "creativeTakeBlock" not in world.calls  # the flag says survival: the menu is not tried at all
    assert used(world, BRICKS) == [(0, 30)]


def test_in_survival_without_commands_it_says_where_the_material_must_come_from(capsys):
    world = HotbarWorld(creative=False, commands=False)
    assert build_plan.cmd_stock(world, plan(stone_bricks=30)) == 6
    assert "клиент не видит" in capsys.readouterr().out


def test_an_old_mod_without_the_tool_falls_back_to_give():
    world = HotbarWorld(creative=True, commands=True, new_mod=False)
    assert build_plan.cmd_stock(world, plan(stone_bricks=30)) == 0
    assert used(world, BRICKS) == [(0, 30)]


def test_creative_stock_reports_why_when_it_cannot():
    survival = HotbarWorld(creative=False)
    assert build_plan.creative_stock(survival, {BRICKS: 10}) == (False, "не творческий режим")
    old = HotbarWorld(new_mod=False)
    taken, why = build_plan.creative_stock(old, {BRICKS: 10})
    assert not taken and "creativeTakeBlock" in why


def test_nothing_is_taken_when_everything_is_already_there(capsys):
    world = HotbarWorld()
    world.slots[0] = (BRICKS, 64)
    assert build_plan.cmd_stock(world, plan(stone_bricks=40)) == 0
    assert "creativeTakeBlock" not in world.calls
    assert "уже у тебя" in capsys.readouterr().out


def test_build_in_creative_mode_no_longer_stops_for_the_materials_it_can_take(monkeypatch):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    monkeypatch.setattr(build_plan, "build_stage", lambda c, part, timeout: ({"done": True}, False))
    monkeypatch.setattr(build_plan, "verify", lambda c, blocks: [])
    monkeypatch.setattr(build_plan, "cmd_cleanup", lambda c, blocks, timeout, plan=None: 0)
    world = HotbarWorld(creative=True, commands=False)
    world_call = world.call
    world.call = lambda name, args: ("{}", False) if name == "getOpenScreen" else world_call(name, args)
    assert build_plan.cmd_build(world, plan(stone_bricks=20, glass=5), timeout=5) == 0
    assert used(world, BRICKS) == [(0, 20)]


def test_build_in_survival_without_the_material_still_refuses(monkeypatch, capsys):
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    world = HotbarWorld(creative=False, commands=False)
    assert build_plan.cmd_build(world, plan(stone_bricks=20), timeout=5) == 6
    assert "НЕ ХВАТАЕТ МАТЕРИАЛОВ" in capsys.readouterr().out
