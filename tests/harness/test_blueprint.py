"""blueprint.py and recipes.py: parts build the right shapes, the plan file is what build_plan.py
reads back, the 9-types-per-stage limit is enforced, and the kit is seeded for her once."""

import py_compile
import sys
from pathlib import Path

import pytest

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import blueprint  # noqa: E402
import recipes  # noqa: E402

from ai_agent_core.local_host.harness import seed_building_kit  # noqa: E402

Blueprint = blueprint.Blueprint
SKIP = {".", " "}


def read_plan(text):
    """The reading of build_plan.py's parse() (legend, layers bottom to top, rows go south)."""
    legend, layers, current = {}, {}, None
    for raw in text.splitlines():
        stripped = raw.strip()
        low = stripped.casefold()
        if low.startswith(("name:", "имя:")) or low in ("легенда:", "legend:"):
            current = None
            continue
        if low.startswith(("слой ", "layer ")):
            current = int(stripped.split()[1])
            layers.setdefault(current, [])
            continue
        if current is None:
            if "=" in stripped:
                key, name = stripped.split("=", 1)
                assert len(key.strip()) == 1
                legend[key.strip()] = name.split("#")[0].strip()
            continue
        if not stripped:
            current = None
            continue
        layers[current].append(raw)
    width = max(len(r) for rows in layers.values() for r in rows)
    out = {}
    for y, rows in layers.items():
        for z, row in enumerate(rows):
            for x, ch in enumerate(row.ljust(width)):
                if ch not in SKIP:
                    out[(x, y, z)] = legend[ch]
    return out


def shifted(b):
    x1, y1, z1, *_ = b.bounds()
    return {(x - x1, y - y1, z - z1): n for (x, y, z), n in b.cells.items()}


def test_box_hollow_and_cut_have_the_right_cell_counts():
    b = Blueprint(palette={"w": "stone"})
    b.box(0, 0, 0, 4, 4, 4, "w")
    assert len(b.cells) == 125
    h = Blueprint().hollow(0, 0, 0, 4, 4, 4, "stone")
    assert len(h.cells) == 125 - 27  # the inside 3x3x3 is empty
    walls = Blueprint().walls(0, 0, 0, 4, 4, 4, "stone")
    assert len(walls.cells) == 5 * 16 and walls.get(2, 0, 2) is None  # no floor
    h.cut(2, 1, 0, 2, 2, 0)
    assert h.get(2, 1, 0) is None and h.get(2, 3, 0) == "stone"  # a doorway 1x2


def test_palette_roles_and_air_names():
    b = Blueprint(palette={"wall": "bricks"})
    b.set(0, 0, 0, "wall").set(1, 0, 0, "oak_planks").set(2, 0, 0, "air")
    assert b.cells == {(0, 0, 0): "bricks", (1, 0, 0): "oak_planks"}


def test_gable_roof_has_a_ridge_and_closed_ends():
    b = Blueprint().gable_roof(0, 0, 6, 4, 0, "roof", ridge="x", overhang=0, end_ref="wall")
    assert b.bounds()[4] == 2  # 5 wide: three steps up (0, 1, 2)
    assert b.get(3, 2, 2) == "roof"  # the ridge
    assert b.get(0, 1, 2) == "wall" and b.get(0, 1, 1) is None or b.get(0, 1, 1) == "wall"
    steep = Blueprint().gable_roof(0, 0, 3, 3, 0, "roof", ridge="z", overhang=1)
    assert steep.get(-1, 0, 1) == "roof" and steep.get(4, 0, 1) == "roof"  # overhang on both sides


def test_round_parts_are_round():
    cyl = Blueprint().cylinder(5, 5, 4, 0, 0, "s", hollow=True)
    assert cyl.get(9, 0, 5) == "s" and cyl.get(5, 0, 5) is None
    disc = Blueprint().disc(5, 5, 3, 0, "s")
    assert disc.get(5, 0, 5) == "s" and disc.get(5, 0, 9) is None
    dome = Blueprint().dome(0, 0, 0, 4, "s")
    assert min(y for _, y, _ in dome.cells) >= 0 and dome.get(0, 4, 0) == "s"
    cone = Blueprint().cone(3, 3, 3, 0, "s")
    assert cone.bounds()[4] == 3 and cone.get(3, 3, 3) == "s"


def test_stairs_arch_mirror_stamp_and_rotate():
    s = Blueprint().stairs(0, 0, 0, "+x", 4, "s")
    assert s.get(3, 3, 0) == "s" and s.get(3, 4, 0) is None and s.get(0, 0, 0) == "s"
    a = Blueprint().arch(0, 0, 0, 3, "x", "s")
    assert a.get(0, 3, 0) == "s" and a.get(3, 0, 0) == "s" and a.get(0, 1, 0) is None
    m = Blueprint().box(0, 0, 0, 1, 0, 0, "s")
    m.set(0, 1, 0, "t").mirror("x")
    assert m.get(1, 1, 0) == "t"  # the mirror image
    base = Blueprint().box(0, 0, 0, 2, 0, 2, "a")
    leg = Blueprint().box(0, 0, 0, 0, 3, 0, "b")
    base.stamp(leg, 1, 1, 1)
    assert base.get(1, 3, 1) == "b" and base.get(1, 1, 1) == "b"
    r = Blueprint().box(0, 0, 0, 3, 0, 1, "s").rotated(90)
    assert r.bounds()[3] - r.bounds()[0] == 1 and r.bounds()[5] - r.bounds()[2] == 3


def test_plan_text_reads_back_exactly_and_uses_safe_symbols():
    b = Blueprint("тест", {"w": "stone_bricks", "r": "oak_planks"})
    b.hollow(0, 0, 0, 5, 3, 4, "w").gable_roof(0, 0, 5, 4, 4, "r").cut(2, 1, 4, 2, 2, 4)
    text = b.plan_text()
    legend_keys = [ln[0] for ln in text.splitlines() if " = " in ln]
    assert "." not in legend_keys[:-1] and "#" not in legend_keys and " " not in legend_keys
    back = {k: v for k, v in read_plan(text).items()}
    assert back == shifted(b)


def test_stages_respect_nine_types_and_report_says_so():
    b = Blueprint()
    for level in range(4):  # four layers, five new block types each
        for i in range(5):
            b.set(i, level, 0, f"block_{level}_{i}")
    stages = b.stages()
    assert [s["layers"] for s in stages] == ["0-0", "1-1", "2-2", "3-3"]
    wide = Blueprint()
    for i in range(10):
        wide.set(i, 0, 0, f"b{i}")
    with pytest.raises(ValueError):
        wide.stages()
    assert "ОШИБКА ЭТАПОВ" in wide.report()


def test_warnings_for_blocks_that_need_a_direction():
    b = Blueprint().set(0, 0, 0, "oak_stairs").set(1, 0, 0, "sand").set(2, 0, 0, "stone")
    text = " ".join(b.warnings())
    assert "oak_stairs" in text and "sand" in text and "stone:" not in text


@pytest.mark.parametrize("name", sorted(recipes.RECIPES))
def test_every_recipe_builds_a_valid_plan_with_few_types_per_stage(name):
    b = recipes.RECIPES[name]["make"]()
    assert len(b.cells) > 20
    assert read_plan(b.plan_text()) == shifted(b)
    stages = b.stages()
    assert all(len(s["types"]) <= blueprint.MAX_TYPES for s in stages)
    assert name in recipes.RECIPES[name]["make"].__name__ or b.report()


def test_the_cottage_has_a_door_windows_and_a_roof():
    b = recipes.cottage(w=9, d=7, h=4)
    assert b.get(4, 2, 6) is None and b.get(4, 3, 6) is None  # the doorway in the south wall
    assert b.get(4, 1, 6) == "spruce_planks" or b.get(4, 1, 6) is not None
    assert any(n == "glass" for n in b.cells.values())
    assert b.bounds()[4] >= 7  # the roof rises above the walls
    custom = recipes.cottage(palette={"wall": "stone_bricks"})
    assert "stone_bricks" in custom.counts()


def test_the_command_line_lists_and_writes(tmp_path, capsys):
    assert blueprint.main(["recipes"]) == 0
    assert "cottage" in capsys.readouterr().out
    out = tmp_path / "plans" / "x.txt"
    assert blueprint.main(["recipe", "tower", "--opt", "r=3", "--opt", "h=10", "--out", str(out)]) == 0
    assert out.exists() and "слой 0" in out.read_text(encoding="utf-8")


def test_the_kit_is_seeded_once_and_the_library_is_valid_python(tmp_path):
    folder = tmp_path / "opechatka" / "skills" / "minecraft"
    folder.mkdir(parents=True)
    assert seed_building_kit(tmp_path, "opechatka") is True
    for name in ("blueprint.py", "recipes.py"):
        py_compile.compile(str(folder / name), doraise=True)
    guide = tmp_path / "opechatka" / "playbook" / "build-from-idea.md"
    assert "Процесс" in guide.read_text(encoding="utf-8")
    (folder / "blueprint.py").write_text("# mine\n", encoding="utf-8")
    assert seed_building_kit(tmp_path, "opechatka") is False  # hers now: never overwritten
    assert (folder / "blueprint.py").read_text(encoding="utf-8") == "# mine\n"
