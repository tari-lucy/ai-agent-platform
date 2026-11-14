"""recipes.py: готовые постройки из деталей blueprint.py. Это образцы: открой, посмотри, как они
собраны из коробок, крыш и вырезов, и составляй свои так же. Каждый рецепт принимает размеры и
палитру ролей (palette={"wall": "stone_bricks"}); роли без значения берут цвета по умолчанию.

    from recipes import cottage, tower
    cottage(w=11, d=7, palette={"roof": "dark_oak_planks"}).save("/workspace/plans/дом.txt")
"""
import math

from blueprint import Blueprint


def _palette(defaults, overrides):
    palette = dict(defaults)
    palette.update(overrides or {})
    return palette


def cottage(w=9, d=7, h=4, palette=None):
    """Дом с фундаментом, угловыми столбами, окнами, дверью, двускатной крышей и трубой."""
    p = _palette({"foundation": "cobblestone", "wall": "oak_planks", "frame": "stripped_oak_log",
                  "floor": "spruce_planks", "roof": "spruce_planks", "glass": "glass"}, palette)
    b = Blueprint(f"дом {w}x{d}", p)
    cx = w // 2
    b.box(0, 0, 0, w - 1, 0, d - 1, "foundation")
    b.floor(1, 1, w - 2, d - 2, 1, "floor")
    b.walls(0, 1, 0, w - 1, h, d - 1, "wall")
    b.columns([(0, 0), (w - 1, 0), (0, d - 1), (w - 1, d - 1)], 1, h, "frame")
    b.floor(1, 1, w - 2, d - 2, h, "floor")  # потолок
    b.cut(cx, 2, d - 1, cx, 3, d - 1)  # дверь (проём 1x2) в южной стене
    b.box(cx - 1, 0, d, cx + 1, 0, d, "foundation")  # крыльцо
    for x in range(2, w - 2, 3):  # окна на южной и северной стенах, кроме места двери
        if abs(x - cx) > 1:
            b.set(x, 3, d - 1, "glass")
        b.set(x, 3, 0, "glass")
    for z in range(2, d - 2, 3):  # окна на боковых стенах
        b.set(0, 3, z, "glass")
        b.set(w - 1, 3, z, "glass")
    b.gable_roof(0, 0, w - 1, d - 1, h + 1, "roof", ridge="x" if w >= d else "z",
                 overhang=1, end_ref="wall")
    b.box(w - 3, 1, 1, w - 3, h + 4, 1, "foundation")  # труба
    return b


def tower(r=3, h=14, cap="cone", palette=None):
    """Круглая башня: стены, перекрытия через каждые 5 слоёв, бойницы, дверь, крыша-конус или зубцы."""
    p = _palette({"foundation": "stone_bricks", "wall": "stone_bricks", "floor": "oak_planks",
                  "trim": "chiseled_stone_bricks", "glass": "glass", "roof": "dark_oak_planks"},
                 palette)
    b = Blueprint(f"башня r{r} h{h}", p)
    c = r + 1
    b.cylinder(c, c, r, 0, 1, "foundation", hollow=False)
    b.cylinder(c, c, r, 2, h, "wall")
    for y in range(5, h, 5):
        b.disc(c, c, r - 1, y, "floor")
    for y in range(4, h, 4):  # бойницы на четыре стороны
        for dx, dz in ((r, 0), (-r, 0), (0, r), (0, -r)):
            b.set(c + dx, y, c + dz, "glass")
    b.cut(c, 2, c + r, c, 3, c + r)  # дверь с юга
    b.cylinder(c, c, r, h, h, "trim", hollow=False)
    if cap == "cone":
        b.cone(c, c, r + 1, h + 1, "roof")
    else:
        b.cylinder(c, c, r + 1, h + 1, h + 1, "wall")
        b.crenellations(c - r - 1, c - r - 1, c + r + 1, c + r + 1, h + 2, "wall")
    return b


def gazebo(r=4, columns=6, palette=None):
    """Беседка: круглый пол, колонны по кругу, кольцевая балка и конусная крыша."""
    p = _palette({"floor": "stone_bricks", "frame": "stripped_spruce_log", "trim": "spruce_planks",
                  "roof": "spruce_planks"}, palette)
    b = Blueprint(f"беседка r{r}", p)
    c = r + 1
    b.disc(c, c, r, 0, "floor")
    points = [(c + (r - 1) * math.cos(2 * math.pi * i / columns),
               c + (r - 1) * math.sin(2 * math.pi * i / columns)) for i in range(columns)]
    b.columns([(round(x), round(z)) for x, z in points], 1, 4, "frame")
    b.disc(c, c, r, 5, "trim", fill=False)
    b.cone(c, c, r + 1, 6, "roof")
    return b


def bridge(length=15, width=3, span=7, palette=None):
    """Арочный мост: настил, перила, опоры и арки под настилом."""
    p = _palette({"deck": "oak_planks", "rail": "oak_planks", "pier": "stone_bricks"}, palette)
    b = Blueprint(f"мост {length}x{width}", p)
    height = max(3, span // 2)
    b.box(0, height, 0, length - 1, height, width - 1, "deck")
    for x in range(0, length, 2):
        b.set(x, height + 1, 0, "rail")
        b.set(x, height + 1, width - 1, "rail")
    for x in range(0, length - 1, span):
        b.box(x, 0, 0, x, height - 1, 0, "pier")
        b.box(x, 0, width - 1, x, height - 1, width - 1, "pier")
        if x + span < length:
            for z in (0, width - 1):
                b.arch(x + span // 2, 0, z, span // 2, "x", "pier")
    b.box(length - 1, 0, 0, length - 1, height - 1, 0, "pier")
    b.box(length - 1, 0, width - 1, length - 1, height - 1, width - 1, "pier")
    return b


def wall_with_gate(length=21, h=5, thick=2, gate=3, palette=None):
    """Крепостная стена с зубцами, воротами по центру и башнями по краям."""
    p = _palette({"wall": "stone_bricks", "tower": "stone_bricks", "trim": "chiseled_stone_bricks"},
                 palette)
    b = Blueprint(f"стена {length}", p)
    b.box(0, 0, 0, length - 1, h, thick - 1, "wall")
    mid = length // 2
    b.cut(mid - gate // 2, 0, 0, mid + gate // 2, h - 2, thick - 1)
    b.box(mid - gate // 2 - 1, h - 1, 0, mid + gate // 2 + 1, h - 1, thick - 1, "trim")
    for x0 in (0, length - 4):
        b.hollow(x0, 0, -1, x0 + 3, h + 3, thick + 1, "tower", top=False)
        b.crenellations(x0, -1, x0 + 3, thick + 1, h + 4, "tower")
    b.crenellations(4, 0, length - 5, thick - 1, h + 1, "wall")
    return b


def fountain(r=3, palette=None):
    """Фонтан: чаша, вода, столб и светильник на вершине."""
    p = _palette({"base": "stone_bricks", "rim": "chiseled_stone_bricks", "water": "water",
                  "pillar": "stone_brick_wall", "light": "sea_lantern"}, palette)
    b = Blueprint(f"фонтан r{r}", p)
    c = r + 1
    b.disc(c, c, r, 0, "base")
    b.disc(c, c, r, 1, "rim", fill=False)
    b.disc(c, c, r - 1, 1, "water")
    b.box(c, 1, c, c, 3, c, "base")
    b.set(c, 4, c, "light")
    return b


def pyramid(size=11, palette=None):
    """Ступенчатая пирамида с входом и внутренней камерой."""
    p = _palette({"stone": "sandstone", "trim": "cut_sandstone"}, palette)
    b = Blueprint(f"пирамида {size}", p)
    b.pyramid(0, 0, size - 1, size - 1, 0, "stone")
    c = size // 2
    b.cut(c, 1, size - 1, c, 2, size - 3)
    b.cut(c - 1, 1, c - 1, c + 1, 3, c + 1)
    b.box(c - 1, 0, size - 2, c + 1, 0, size - 1, "trim")
    return b


RECIPES = {
    "cottage": {"make": cottage, "about": "дом с крышей, окнами, дверью, трубой",
                "params": "w, d, h", "roles": "foundation wall frame floor roof glass"},
    "tower": {"make": tower, "about": "круглая башня (cap=cone или crenel)",
              "params": "r, h, cap", "roles": "foundation wall floor trim glass roof"},
    "gazebo": {"make": gazebo, "about": "беседка с колоннами и конусной крышей",
               "params": "r, columns", "roles": "floor frame trim roof"},
    "bridge": {"make": bridge, "about": "арочный мост с перилами",
               "params": "length, width, span", "roles": "deck rail pier"},
    "wall": {"make": wall_with_gate, "about": "крепостная стена с воротами и башнями",
             "params": "length, h, thick, gate", "roles": "wall tower trim"},
    "fountain": {"make": fountain, "about": "фонтан с чашей и светильником",
                 "params": "r", "roles": "base rim water pillar light"},
    "pyramid": {"make": pyramid, "about": "ступенчатая пирамида с камерой",
                "params": "size", "roles": "stone trim"},
}
