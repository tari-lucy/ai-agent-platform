#!/usr/bin/env python3
"""design_check.py: проверка формы чертежа и картинка чертежа до стройки.

Библиотека без зависимостей (только стандартная). Клетки: {(x, y, z): "имя_блока"}.

    findings = lint(cells)          # список Finding(level, text); level: "ошибка" | "замечание"
    save_views(cells, "/workspace/shots/plan.png")   # лист 2x2: четыре стороны

Ошибки мешают стройке (висящие в воздухе блоки): build_plan.py откажется строить, пока
ты их не исправишь. Замечания показывают, на что посмотреть: это не запреты, а вопросы
(асимметрия может быть задумкой, но «почти симметрично» чаще означает опечатку).
"""
import struct
import zlib
from collections import Counter, deque, namedtuple

Finding = namedtuple("Finding", "level text")

AIR = {None, "", "air", "воздух", "пропуск", "skip"}
LIGHT = ("glowstone", "sea_lantern", "lantern", "torch", "shroomlight", "redstone_lamp",
         "jack_o_lantern", "froglight", "campfire", "beacon")
NEIGHBOURS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
SIDES = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
SHOW = 5  # how many examples one finding lists


def _short(name):
    return str(name).split(":")[-1]


def _near(points):
    return ", ".join(f"({x},{y},{z})" for x, y, z in sorted(points)[:SHOW]) + (
        f" и ещё {len(points) - SHOW}" if len(points) > SHOW else "")


def floating(cells):
    """Groups of blocks that touch nothing connected to the lowest layer (they would hang in the air)."""
    if not cells:
        return []
    low = min(y for _, y, _ in cells)
    seen, groups = set(), []
    for start in cells:
        if start in seen:
            continue
        group, queue, grounded = [], deque([start]), False
        seen.add(start)
        while queue:
            x, y, z = queue.popleft()
            group.append((x, y, z))
            grounded = grounded or y == low
            for dx, dy, dz in NEIGHBOURS:
                nxt = (x + dx, y + dy, z + dz)
                if nxt in cells and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if not grounded:
            groups.append(group)
    return groups


def lint(cells):
    """Checks of shape and consistency; the technical ones (direction, gravity) live in Blueprint.warnings()."""
    cells = {p: n for p, n in cells.items() if str(n).strip().casefold() not in AIR}
    out = []
    if not cells:
        return [Finding("ошибка", "чертёж пуст")]

    for group in floating(cells):
        out.append(Finding("ошибка", f"висит в воздухе ({len(group)} бл.): {_near(group)}; "
                                     "добавь опору или колонну, иначе стройка не сойдётся"))

    xs, ys, zs = zip(*cells)
    x1, x2, z1, z2 = min(xs), max(xs), min(zs), max(zs)

    # Mirror symmetry along each axis: "almost symmetrical" is more often a typo than an intention.
    for axis, label in ((0, "по оси X (лево и право)"), (2, "по оси Z (перёд и зад)")):
        lo, hi = (x1, x2) if axis == 0 else (z1, z2)
        same, odd = 0, []
        for (x, y, z), name in cells.items():
            mirror = (lo + hi - x, y, z) if axis == 0 else (x, y, lo + hi - z)
            if cells.get(mirror) == name:
                same += 1
            else:
                odd.append((x, y, z))
        share = same / len(cells)
        if 0.8 <= share < 1.0 and len(cells) >= 30:
            out.append(Finding("замечание", f"почти симметрично {label} ({share:.0%}), без пары: {_near(odd)}; "
                                            "если это не задумка, поправь"))

    # One-cell holes in a floor, a roof or a ledge: all four sides filled, the cell itself empty.
    holes = []
    for x in range(x1, x2 + 1):
        for y in range(min(ys), max(ys) + 1):
            for z in range(z1, z2 + 1):
                if (x, y, z) in cells:
                    continue
                if all((x + dx, y, z + dz) in cells for dx, _, dz in SIDES):
                    holes.append((x, y, z))
    if holes:
        out.append(Finding("замечание", f"дырка в один блок среди сплошного: {_near(holes)}; "
                                        "окно это или пропуск, который надо заделать?"))

    # Light that is walled in does not light anything.
    shut = [p for p, n in cells.items() if any(k in _short(n) for k in LIGHT)
            and all((p[0] + dx, p[1] + dy, p[2] + dz) in cells for dx, dy, dz in NEIGHBOURS)]
    if shut:
        out.append(Finding("замечание", f"светильник замурован со всех сторон: {_near(shut)}; "
                                        "он не светит, перенеси под крышу или на столб"))
    elif len(cells) >= 200 and not any(any(k in _short(n) for k in LIGHT) for n in cells.values()):
        out.append(Finding("замечание", "нет ни одного источника света: ночью внутри и вокруг будут мобы"))

    # A lone block sticking out of the top: a chimney is fine, a stray block is not.
    top = {}
    for x, y, z in cells:
        top[(x, z)] = max(y, top.get((x, z), y))
    spikes = []
    for (x, z), y in top.items():
        around = [top.get((x + dx, z + dz)) for dx, _, dz in SIDES]
        if all(t is None or t < y for t in around) and (x, y - 1, z) in cells and y - 1 >= min(ys) + 2:
            spikes.append((x, y, z))
    if spikes and len(spikes) <= 4:
        out.append(Finding("замечание", f"одиночный блок торчит выше соседей: {_near(spikes)}; "
                                        "труба, шпиль или лишний блок?"))

    # Proportions: a roof taller than the walls or posts it sits on reads as heavy.
    area = {y: sum(1 for _, yy, _ in cells if yy == y) for y in range(min(ys), max(ys) + 1)}
    levels = sorted(area)
    if len(levels) >= 5:
        start = levels[-1]
        while start - 1 in area and area[start - 1] >= area[start] and area[start - 1] >= 0.8 * area[start]:
            start -= 1
        roof, walls = levels[-1] - start + 1, start - (levels[0] + 1)
        if roof >= 3 and walls >= 1 and roof > walls + 1:
            out.append(Finding("замечание", f"крыша ({roof} слоёв) выше стен или опор ({walls}): выглядит "
                                            "тяжело; подними опоры или сделай крышу ниже и площе"))

    # A big top surface in one material is flat to the eye; a rim, a ridge or a pattern fixes that.
    names_on_top = [cells[(x, y, z)] for (x, z), y in top.items()]
    if len(names_on_top) >= 60:
        name, count = Counter(names_on_top).most_common(1)[0]
        if count / len(names_on_top) > 0.7:
            out.append(Finding("замечание", f"{count / len(names_on_top):.0%} видимой сверху поверхности это "
                                            f"{_short(name)}: добавь окантовку, конёк или узор другим блоком"))

    kinds = Counter(_short(n) for n in cells.values())
    if len(kinds) > 5:
        out.append(Finding("замечание", f"{len(kinds)} видов материала: больше пяти выглядит пёстро"))
    name, count = kinds.most_common(1)[0]
    if len(cells) >= 100 and count / len(cells) > 0.85:
        out.append(Finding("замечание", f"{name} это {count / len(cells):.0%} блоков: добавь контрастный "
                                        "материал для крыши, рамы или основания"))
    return out


# ------------------------------------------------------------------ picture ---

COLOURS = (
    ("glowstone", (250, 210, 100)), ("lantern", (250, 200, 90)), ("sea_lantern", (190, 225, 215)),
    ("glass", (170, 210, 225)), ("water", (60, 100, 200)), ("lava", (230, 100, 20)),
    ("stone_brick", (122, 122, 122)), ("cobblestone", (118, 118, 118)), ("deepslate", (70, 70, 76)),
    ("stone", (136, 136, 136)), ("andesite", (140, 140, 140)), ("granite", (160, 110, 95)),
    ("diorite", (190, 190, 190)), ("brick", (150, 86, 70)), ("sandstone", (216, 203, 155)),
    ("sand", (219, 207, 163)), ("quartz", (232, 228, 220)), ("terracotta", (152, 94, 68)),
    ("concrete", (180, 180, 180)), ("wool", (232, 232, 232)), ("obsidian", (28, 20, 44)),
    ("dark_oak", (66, 43, 20)), ("spruce", (114, 84, 48)), ("birch", (196, 179, 123)),
    ("jungle", (160, 115, 80)), ("acacia", (168, 90, 50)), ("cherry", (226, 178, 172)),
    ("mangrove", (117, 54, 48)), ("bamboo", (193, 173, 80)), ("crimson", (110, 50, 70)),
    ("warped", (40, 110, 110)), ("oak", (162, 130, 78)), ("leaves", (60, 120, 40)),
    ("grass", (96, 150, 60)), ("dirt", (134, 96, 67)), ("iron", (200, 200, 200)),
    ("gold", (240, 200, 60)), ("copper", (190, 110, 80)), ("coal", (40, 40, 40)),
)
LOG_DARKEN = 0.78  # logs and stripped logs read darker than the planks of the same wood
SKY = (226, 234, 242)


def colour(name):
    low = _short(name).casefold()
    for key, rgb in COLOURS:
        if key in low:
            if ("log" in low or "wood" in low or "stem" in low) and "plank" not in low:
                return tuple(int(c * LOG_DARKEN) for c in rgb)
            return rgb
    seed = sum(ord(ch) * (i + 3) for i, ch in enumerate(low))
    return (90 + seed * 37 % 140, 90 + seed * 59 % 140, 90 + seed * 83 % 140)


def _shade(rgb, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


class Canvas:
    def __init__(self, width, height, background=SKY):
        self.w, self.h = width, height
        self.px = bytearray(bytes(background) * (width * height))

    def set(self, x, y, rgb):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(rgb)

    def polygon(self, points, rgb):
        """Fill of a convex polygon by scanlines."""
        ys = [p[1] for p in points]
        for y in range(max(0, int(min(ys))), min(self.h - 1, int(max(ys))) + 1):
            xs = []
            for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
                if ay == by or not (min(ay, by) <= y + 0.5 < max(ay, by)):
                    continue
                xs.append(ax + (y + 0.5 - ay) * (bx - ax) / (by - ay))
            if len(xs) >= 2:
                for x in range(max(0, int(round(min(xs)))), min(self.w - 1, int(round(max(xs)))) + 1):
                    self.set(x, y, rgb)

    def line(self, a, b, rgb):
        (x0, y0), (x1, y1) = (int(round(a[0])), int(round(a[1]))), (int(round(b[0])), int(round(b[1])))
        dx, dy, sx, sy = abs(x1 - x0), -abs(y1 - y0), 1 if x0 < x1 else -1, 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0, rgb)
            if (x0, y0) == (x1, y1):
                break
            e2 = 2 * err
            if e2 >= dy:
                err, x0 = err + dy, x0 + sx
            if e2 <= dx:
                err, y0 = err + dx, y0 + sy

    def blit(self, other, ox, oy):
        for row in range(other.h):
            start = row * other.w * 3
            at = ((oy + row) * self.w + ox) * 3
            self.px[at:at + other.w * 3] = other.px[start:start + other.w * 3]

    def png(self):
        raw = b"".join(b"\x00" + bytes(self.px[r * self.w * 3:(r + 1) * self.w * 3]) for r in range(self.h))

        def chunk(kind, data):
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _rotated(cells, turns):
    xs, _, zs = zip(*cells)
    x1, z1 = min(xs), min(zs)
    width, depth = max(xs) - x1 + 1, max(zs) - z1 + 1
    out = {}
    for (x, y, z), name in cells.items():
        nx, nz = x - x1, z - z1
        for _ in range(turns):
            nx, nz, width, depth = nz, width - 1 - nx, depth, width
        out[(nx, y, nz)] = name
    return out


def _view(cells, size):
    """One oblique view from the +x +z side: the top and the two faces that face the viewer."""
    a, b, v = size * 0.866, size * 0.5, size  # half width, half height of a cube's top, cube height
    ymin = min(y for _, y, _ in cells)
    pad = 12
    corners = [(x, y, z) for x, y, z in cells]
    sx = [(x - z) * a for x, y, z in corners] + [(x + 1 - z) * a for x, y, z in corners] + [(x - z - 1) * a for x, y, z in corners]
    sy = [(x + z) * b - (y + 1 - ymin) * v for x, y, z in corners] + [(x + z + 2) * b - (y - ymin) * v for x, y, z in corners]
    left, top = min(sx), min(sy)
    width, height = int(max(sx) - left) + 2 * pad, int(max(sy) - top) + 2 * pad
    canvas = Canvas(width, height)

    def project(x, y, z):
        return ((x - z) * a - left + pad, (x + z) * b - (y - ymin) * v - top + pad)

    for (x, y, z) in sorted(cells, key=lambda p: (p[0] + p[2], p[1])):
        rgb = colour(cells[(x, y, z)])
        faces = []
        if (x, y + 1, z) not in cells:
            faces.append(([project(x, y + 1, z), project(x + 1, y + 1, z), project(x + 1, y + 1, z + 1),
                           project(x, y + 1, z + 1)], 1.0))
        if (x + 1, y, z) not in cells:
            faces.append(([project(x + 1, y, z), project(x + 1, y, z + 1), project(x + 1, y + 1, z + 1),
                           project(x + 1, y + 1, z)], 0.8))
        if (x, y, z + 1) not in cells:
            faces.append(([project(x, y, z + 1), project(x + 1, y, z + 1), project(x + 1, y + 1, z + 1),
                           project(x, y + 1, z + 1)], 0.62))
        for points, factor in faces:
            canvas.polygon(points, _shade(rgb, factor))
            edge = _shade(rgb, factor * 0.55)
            for p, q in zip(points, points[1:] + points[:1]):
                canvas.line(p, q, edge)
    return canvas


def render_views(cells, size=None):
    """Four sides on one sheet, 2x2: top left SE, top right SW, bottom left NW, bottom right NE."""
    cells = {p: n for p, n in cells.items() if str(n).strip().casefold() not in AIR}
    if not cells:
        raise ValueError("чертёж пуст")
    xs, ys, zs = zip(*cells)
    extent = max(max(xs) - min(xs) + 1, max(zs) - min(zs) + 1, max(ys) - min(ys) + 1)
    size = size or max(6, min(28, int(420 / extent)))
    views = [_view(_rotated(cells, k), size) for k in range(4)]
    cell_w, cell_h = max(v.w for v in views), max(v.h for v in views)
    sheet = Canvas(cell_w * 2 + 6, cell_h * 2 + 6, background=(40, 44, 52))
    for k, view in enumerate(views):
        sheet.blit(view, (k % 2) * (cell_w + 6) + (cell_w - view.w) // 2, (k // 2) * (cell_h + 6) + (cell_h - view.h) // 2)
    return sheet


def save_views(cells, path, size=None):
    sheet = render_views(cells, size)
    with open(path, "wb") as stream:
        stream.write(sheet.png())
    return sheet.w, sheet.h
