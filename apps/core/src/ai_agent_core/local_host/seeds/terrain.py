#!/usr/bin/env python3
"""terrain.py: выбрать площадку под постройку и посчитать выравнивание. Без зависимостей.

Данные берутся из getHeightmap мода (одна карта высот за вызов). Здесь только расчёт:

    heights = Heights.from_reply(reply)                    # ответ getHeightmap
    sites = find_sites(heights, 7, 9, near=(229, 28))       # лучшие окна под дом 7x9 с запасом
    plan = level_plan(heights, x, z, 7, 9, ground=71)       # что досыпать и что срезать

Что значит «хорошая площадка»: ровная (мало работы по выравниванию), без воды и лавы, без деревьев
(их придётся рубить), близко к нужной точке. Выравнивание идёт к одной высоте: ниже неё досыпают,
выше срезают; срез в 2,5 раза «дороже» засыпки (копать долго, а цоколь выглядит естественно).
"""
from collections import namedtuple

UNKNOWN = -9999
FILL_COST, CUT_COST, COVER_COST, WATER_COST = 1.0, 2.5, 3.0, 6.0
LAVA = ("lava", "magma")
WATER = ("water", "ice", "kelp", "seagrass")
COVER = ("leaves", "_log", "_wood", "_stem", "vine")

Site = namedtuple("Site", "x z ground fill cut cover water score span")


class Heights:
    """The map of getHeightmap: ground height, and what lies on top, per column."""

    def __init__(self, x1, z1, width, depth, palette, y, ids, gy, gids):
        self.x1, self.z1, self.width, self.depth = x1, z1, width, depth
        self.palette, self._y, self._ids, self._gy, self._gids = palette, y, ids, gy, gids

    @classmethod
    def from_reply(cls, reply):
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise ValueError(f"карта высот не получена: {reply}")
        return cls(reply["x1"], reply["z1"], reply["width"], reply["depth"], reply["palette"],
                   reply["y"], reply["id"], reply["gy"], reply["gid"])

    def inside(self, x, z):
        return self.x1 <= x < self.x1 + self.width and self.z1 <= z < self.z1 + self.depth

    def _i(self, x, z):
        return (z - self.z1) * self.width + (x - self.x1)

    def known(self, x, z):
        return self.inside(x, z) and self._gy[self._i(x, z)] != UNKNOWN

    def ground(self, x, z):
        return self._gy[self._i(x, z)]

    def top(self, x, z):
        return self._y[self._i(x, z)]

    def ground_name(self, x, z):
        k = self._gids[self._i(x, z)]
        return self.palette[k] if k >= 0 else ""

    def top_name(self, x, z):
        k = self._ids[self._i(x, z)]
        return self.palette[k] if k >= 0 else ""

    def kind(self, x, z):
        """'lava' | 'water' | 'cover' (tree) | 'ground' for the top of a column."""
        top = self.top_name(x, z)
        ground = self.ground_name(x, z)
        if any(k in top or k in ground for k in LAVA):
            return "lava"
        if any(k in top for k in WATER) and self.top(x, z) >= self.ground(x, z):
            return "water"
        if any(k in top for k in COVER):
            return "cover"
        return "ground"


def merge(tiles):
    """Join the tiles of several getHeightmap calls into one map (the union rectangle; gaps stay unknown)."""
    x1, z1 = min(h.x1 for h in tiles), min(h.z1 for h in tiles)
    x2, z2 = max(h.x1 + h.width for h in tiles), max(h.z1 + h.depth for h in tiles)
    width, depth = x2 - x1, z2 - z1
    palette, index = [], {}
    y, gy = [UNKNOWN] * (width * depth), [UNKNOWN] * (width * depth)
    ids, gids = [-1] * (width * depth), [-1] * (width * depth)

    def pal(h, k):
        if k < 0:
            return -1
        name = h.palette[k]
        if name not in index:
            index[name] = len(palette)
            palette.append(name)
        return index[name]

    for h in tiles:
        for z in range(h.z1, h.z1 + h.depth):
            for xx in range(h.x1, h.x1 + h.width):
                i, j = (z - z1) * width + (xx - x1), h._i(xx, z)
                y[i], gy[i] = h._y[j], h._gy[j]
                ids[i], gids[i] = pal(h, h._ids[j]), pal(h, h._gids[j])
    return Heights(x1, z1, width, depth, palette, y, ids, gy, gids)


def _window_cost(h, x, z, w, d):
    """Cost of making the w x d window (corner x, z) level, or None if it cannot be a site."""
    cols = []
    for zz in range(z, z + d):
        for xx in range(x, x + w):
            if not h.known(xx, zz):
                return None
            cols.append((xx, zz))
    kinds = [h.kind(xx, zz) for xx, zz in cols]
    if "lava" in kinds:
        return None
    grounds = sorted(h.ground(xx, zz) for xx, zz in cols)
    water = kinds.count("water")
    cover = kinds.count("cover")
    best = None
    for target in range(grounds[0], grounds[-1] + 1):
        fill = sum(max(0, target - g) for g in grounds)
        cut = sum(max(0, g - target) for g in grounds)
        cost = fill * FILL_COST + cut * CUT_COST
        if best is None or cost < best[0]:
            best = (cost, target, fill, cut)
    cost, target, fill, cut = best
    cost += cover * COVER_COST + water * WATER_COST
    return cost, target, fill, cut, cover, water, grounds[-1] - grounds[0]


def find_sites(h, width, depth, margin=2, near=None, top=5, max_span=4):
    """The best windows for a width x depth building with `margin` blocks of room on every side.

    Returns Site(x, z, ground, fill, cut, cover, water, score, span) of the BUILDING footprint (the margin
    is judged but not returned); `ground` is the height of the top solid block under the floor, so the
    floor (layer 0) goes at y = ground + 1. Sorted best first; windows steeper than max_span are dropped."""
    out = []
    ww, dd = width + 2 * margin, depth + 2 * margin
    for z in range(h.z1, h.z1 + h.depth - dd + 1):
        for x in range(h.x1, h.x1 + h.width - ww + 1):
            got = _window_cost(h, x, z, ww, dd)
            if got is None:
                continue
            cost, target, fill, cut, cover, water, span = got
            if span > max_span:
                continue
            cx, cz = x + ww / 2, z + dd / 2
            far = 0.0 if near is None else ((cx - near[0]) ** 2 + (cz - near[1]) ** 2) ** 0.5 * 0.25
            out.append(Site(x + margin, z + margin, target, fill, cut, cover, water, round(cost + far, 1), span))
    out.sort(key=lambda s: s.score)
    picked = []
    for s in out:   # candidates that are not just a shifted copy of a better one
        if all(abs(s.x - p.x) >= width or abs(s.z - p.z) >= depth for p in picked):
            picked.append(s)
        if len(picked) >= top:
            break
    return picked


def level_plan(h, x, z, width, depth, ground, margin=1, material="dirt", cap="grass_block"):
    """What to do so that the footprint (x, z, width, depth, plus `margin`) is level at `ground`.

    -> {"fill": [(x, y, z, block)], "cut": [(x, y, z)]}. Fill is placed bottom up with `material` and a
    `cap` on top; cut lists every solid cell above `ground` (hills, trees, leaves), top down."""
    fill, cut = [], []
    for zz in range(z - margin, z + depth + margin):
        for xx in range(x - margin, x + width + margin):
            if not h.known(xx, zz):
                raise ValueError(f"столбец {xx},{zz} не прогружен: подойди ближе и повтори")
            g, top = h.ground(xx, zz), h.top(xx, zz)
            for y in range(g + 1, ground + 1):
                fill.append((xx, y, zz, cap if y == ground and cap else material))
            for y in range(max(top, g), ground, -1):
                cut.append((xx, y, zz))
    fill.sort(key=lambda c: (c[1], c[2], c[0]))
    cut.sort(key=lambda c: (-c[1], c[2], c[0]))
    return {"fill": fill, "cut": cut}


def describe(site, width, depth):
    """One line a person (or she) can act on."""
    floor = site.ground + 1
    work = []
    if site.fill:
        work.append(f"досыпать {site.fill}")
    if site.cut:
        work.append(f"срезать {site.cut}")
    if site.cover:
        work.append(f"убрать деревьев в {site.cover} столбцах")
    if site.water:
        work.append(f"вода в {site.water} столбцах")
    return (f"--origin {site.x},{floor},{site.z}  ({width}x{depth}, перепад {site.span}, работа: "
            + (", ".join(work) if work else "ровно, готово") + f", оценка {site.score})")
