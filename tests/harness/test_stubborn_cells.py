"""Cells the builder handed back twice are not given to it again for a while, and are listed instead
(2026-11-19: the same three wall cells went back to the builder stage after stage, 61 NOSTAND lines)."""

import json
import sys
import time
from pathlib import Path

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402

A, B = (1, 64, 1, "minecraft:stone"), (2, 64, 1, "minecraft:stone")


class Builder:
    """buildBlocks remembers what it was given; the queue status says which cells came back."""

    def __init__(self, defer):
        self.defer, self.given = set(defer), []

    def call(self, name, args):
        if name == "buildBlocks":
            self.given.append([tuple(b[:3]) for b in json.loads(args["blocks"])])
            return json.dumps({"ok": True}), False
        raise AssertionError(name)


def run(monkeypatch, tmp_path, builder, part):
    monkeypatch.setattr(build_plan, "STUBBORN_FILE", str(tmp_path / "stubborn.json"))
    given = builder.given

    def wait(c, timeout, **_):
        last = given[-1]
        return {"deferred": [f"{x},{y},{z}" for x, y, z in last if (x, y, z) in builder.defer], "walkDebug": "NOSTAND(...)"}

    monkeypatch.setattr(build_plan, "wait_queue", wait)
    return build_plan.build_stage(builder, part, 5)


def test_a_cell_handed_back_twice_is_skipped_and_listed(monkeypatch, tmp_path, capsys):
    builder = Builder(defer={A[:3]})
    for _ in range(2):
        run(monkeypatch, tmp_path, builder, [A, B])
    assert builder.given == [[A[:3], B[:3]], [A[:3], B[:3]]]
    status, jammed = run(monkeypatch, tmp_path, builder, [A, B])
    assert builder.given[-1] == [B[:3]]          # the third time A is not given
    assert "1,64,1" in status["deferred"] and not jammed  # but it is still reported as not built
    assert "Пропускаю 1 клеток" in capsys.readouterr().out


def test_a_stage_of_only_stubborn_cells_does_not_call_the_builder(monkeypatch, tmp_path):
    builder = Builder(defer={A[:3]})
    for _ in range(2):
        run(monkeypatch, tmp_path, builder, [A])
    calls = len(builder.given)
    status, _ = run(monkeypatch, tmp_path, builder, [A])
    assert len(builder.given) == calls and status["skipped"] == 1


def test_a_cell_that_stands_is_forgotten_and_entries_expire(monkeypatch, tmp_path):
    builder = Builder(defer={A[:3]})
    run(monkeypatch, tmp_path, builder, [A])
    builder.defer = set()                       # placed now (a support, a newer mod)
    run(monkeypatch, tmp_path, builder, [A])
    assert build_plan.stubborn_load() == {}
    builder.defer = {A[:3]}
    run(monkeypatch, tmp_path, builder, [A])
    run(monkeypatch, tmp_path, builder, [A])
    data = json.loads((tmp_path / "stubborn.json").read_text())
    data["1,64,1"]["at"] = time.time() - build_plan.STUBBORN_TTL - 1
    (tmp_path / "stubborn.json").write_text(json.dumps(data))
    assert build_plan.stubborn_split([A]) == ([A], [])


def test_retry_flag_clears_the_list(monkeypatch, tmp_path):
    monkeypatch.setattr(build_plan, "STUBBORN_FILE", str(tmp_path / "stubborn.json"))
    build_plan.stubborn_save({"1,64,1": {"tries": 5, "at": time.time(), "block": "minecraft:stone"}})
    plan = tmp_path / "p.txt"
    plan.write_text("легенда:\n# = stone\nслой 0\n#\n", encoding="utf-8")
    build_plan.main(["preview", str(plan), "--retry-stubborn"])
    assert build_plan.stubborn_load() == {}
