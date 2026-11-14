#!/usr/bin/env python3
"""blueprint.py: от идеи к схеме для build_plan.py. Универсальный конструктор из простых деталей.

Ты описываешь постройку кодом (детали и размеры), библиотека собирает слои, проверяет материалы и
пишет файл, который строит build_plan.py. Считать клетки руками не нужно.

    import sys; sys.path.insert(0, "/workspace/skills/minecraft")
    from blueprint import Blueprint
    b = Blueprint("домик", palette={"wall": "stone_bricks", "roof": "spruce_planks"})
    b.hollow(0, 0, 0, 8, 4, 6, "wall")                 # коробка 9x5x7 (стены, пол, потолок)
    b.cut(4, 1, 6, 4, 2, 6)                            # проём двери (2 блока) в южной стене
    b.gable_roof(0, 0, 8, 6, 5, "roof", ridge="x", overhang=1)
    print(b.report())                                  # размеры, материалы, этапы, предупреждения
    b.save("/workspace/plans/домик.txt")

Координаты: x на восток, y вверх, z на юг, от любого начала (в файле всё сдвигается в 0,0,0).
Блок можно назвать ролью из палитры («wall») или именем («oak_planks»). «air» или None убирает
блок (вырезает проём). Блоки только простые, без состояний (лестница, дверь, забор будут
стоять в положении по умолчанию): для скатов крыши используй gable_roof/hip_roof (ступени из
целых блоков), для дверей и окон вырезай проёмы и вставляй стекло.

Команды из консоли:
    python3 blueprint.py recipes                          список готовых рецептов
    python3 blueprint.py recipe cottage --opt w=11 --opt d=7 --palette wall=stone_bricks --out plans/x.txt
    python3 blueprint.py show plans/x.txt                 слои и материалы готового файла
"""
import argparse
import json
import math
import os
import sys

# Символы легенды (нельзя '.', '#', ' ', '='): '.' и ' ' в схеме значат «пропуск».
SYMBOLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@$%&*+~^!?:;<>/"
AIR = {None, "", "air", "воздух", "пропуск", "skip"}
MAX_TYPES = 9  # видов блоков на этап (хотбар)
NEEDS_STATE = ("stairs", "door", "trapdoor", "bed", "button", "lever", "sign", "banner", "fence_gate",
               "torch", "lantern", "chain", "vine", "ladder", "anvil", "pane", "bars", "wall")
GRAVITY = ("sand", "gravel", "concrete_powder", "anvil")


def _r(value):
    return int(round(value))


class Blueprint:
    def __init__(self, name="постройка", palette=None):
        self.name = name
        self.palette = dict(palette or {})
        self.cells = {}  # (x, y, z) -> имя блока

    # --- базовое -------------------------------------------------------------------------
    def block(self, ref):
        if ref is None:
            return None
        ref = self.palette.get(ref, ref)
        return None if str(ref).strip().casefold() in AIR else str(ref).strip()

    def set(self, x, y, z, ref):
        name = self.block(ref)
        key = (_r(x), _r(y), _r(z))
        if name is None:
            self.cells.pop(key, None)
        else:
            self.cells[key] = name
        return self

    def get(self, x, y, z):
        return self.cells.get((_r(x), _r(y), _r(z)))

    def cut(self, x1, y1, z1, x2, y2, z2):
        """Вырезать (убрать блоки) в параллелепипеде: проёмы дверей, окон, арок."""
        return self.box(x1, y1, z1, x2, y2, z2, None)

    # --- объёмные детали -----------------------------------------------------------------
    def box(self, x1, y1, z1, x2, y2, z2, ref):
        for x in range(min(_r(x1), _r(x2)), max(_r(x1), _r(x2)) + 1):
            for y in range(min(_r(y1), _r(y2)), max(_r(y1), _r(y2)) + 1):
                for z in range(min(_r(z1), _r(z2)), max(_r(z1), _r(z2)) + 1):
                    self.set(x, y, z, ref)
        return self

    def hollow(self, x1, y1, z1, x2, y2, z2, ref, thick=1, top=True, bottom=True):
        """Пустая коробка: стены толщиной thick, пол и потолок (можно отключить)."""
        xa, xb = sorted((_r(x1), _r(x2)))
        ya, yb = sorted((_r(y1), _r(y2)))
        za, zb = sorted((_r(z1), _r(z2)))
        for x in range(xa, xb + 1):
            for y in range(ya, yb + 1):
                for z in range(za, zb + 1):
                    wall = (x - xa < thick or xb - x < thick or z - za < thick or zb - z < thick)
                    lid = (top and yb - y < thick) or (bottom and y - ya < thick)
                    if wall or lid:
                        self.set(x, y, z, ref)
        return self

    def walls(self, x1, y1, z1, x2, y2, z2, ref, thick=1):
        """Только вертикальные стены по периметру (без пола и потолка)."""
        return self.hollow(x1, y1, z1, x2, y2, z2, ref, thick, top=False, bottom=False)

    def floor(self, x1, z1, x2, z2, y, ref):
        return self.box(x1, y, z1, x2, y, z2, ref)

    def line(self, x1, y1, z1, x2, y2, z2, ref):
        """Прямая линия в пространстве (балки, перила, склоны)."""
        steps = max(abs(_r(x2) - _r(x1)), abs(_r(y2) - _r(y1)), abs(_r(z2) - _r(z1)), 1)
        for i in range(steps + 1):
            t = i / steps
            self.set(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, z1 + (z2 - z1) * t, ref)
        return self

    def disc(self, cx, cz, r, y, ref, fill=True):
        """Круг (fill) или кольцо в горизонтальной плоскости на высоте y."""
        for x in range(_r(cx - r) - 1, _r(cx + r) + 2):
            for z in range(_r(cz - r) - 1, _r(cz + r) + 2):
                d = math.hypot(x - cx, z - cz)
                if d <= r + 0.3 and (fill or d >= r - 0.7):
                    self.set(x, y, z, ref)
        return self

    def cylinder(self, cx, cz, r, y1, y2, ref, hollow=True, thick=1):
        for y in range(min(_r(y1), _r(y2)), max(_r(y1), _r(y2)) + 1):
            for x in range(_r(cx - r) - 1, _r(cx + r) + 2):
                for z in range(_r(cz - r) - 1, _r(cz + r) + 2):
                    d = math.hypot(x - cx, z - cz)
                    if d <= r + 0.3 and (not hollow or d >= r + 0.3 - thick - 0.4):
                        self.set(x, y, z, ref)
        return self

    def sphere(self, cx, cy, cz, r, ref, hollow=True, thick=1, half=False):
        """Шар; half=True оставляет только верхнюю половину (купол)."""
        for x in range(_r(cx - r) - 1, _r(cx + r) + 2):
            for y in range(_r(cy - r) - 1, _r(cy + r) + 2):
                for z in range(_r(cz - r) - 1, _r(cz + r) + 2):
                    if half and y < cy:
                        continue
                    d = math.sqrt((x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2)
                    if d <= r + 0.3 and (not hollow or d >= r + 0.3 - thick - 0.4):
                        self.set(x, y, z, ref)
        return self

    def dome(self, cx, cy, cz, r, ref, thick=1):
        return self.sphere(cx, cy, cz, r, ref, hollow=True, thick=thick, half=True)

    def cone(self, cx, cz, r, y, ref, hollow=False):
        """Конус-крыша башни: кольца уменьшающегося радиуса вверх от высоты y."""
        level = 0
        while r - level >= 0:
            self.disc(cx, cz, r - level, y + level, ref, fill=not hollow or r - level < 1)
            level += 1
        return self

    def pyramid(self, x1, z1, x2, z2, y, ref, hollow=False):
        xa, xb = sorted((_r(x1), _r(x2)))
        za, zb = sorted((_r(z1), _r(z2)))
        level = 0
        while xa + level <= xb - level and za + level <= zb - level:
            args = (xa + level, y + level, za + level, xb - level, y + level, zb - level, ref)
            if hollow:
                self.walls(*args)
            else:
                self.box(*args)
            level += 1
        return self

    # --- крыши ---------------------------------------------------------------------------
    def gable_roof(self, x1, z1, x2, z2, y, ref, ridge="x", overhang=1, end_ref=None):
        """Двускатная крыша ступенями из целых блоков. ridge: вдоль какой оси идёт конёк.
        end_ref: чем заполнить торцевые треугольники (None: оставить открытыми)."""
        xa, xb = sorted((_r(x1), _r(x2)))
        za, zb = sorted((_r(z1), _r(z2)))
        oh = int(overhang)
        along_x = ridge == "x"
        a1, a2 = (xa - oh, xb + oh) if along_x else (za - oh, zb + oh)
        b1, b2 = (za, zb) if along_x else (xa, xb)
        level = 0
        while True:
            lo, hi = b1 - oh + level, b2 + oh - level
            if lo > hi:
                break
            for a in range(a1, a2 + 1):
                for edge in {lo, hi}:
                    x, z = (a, edge) if along_x else (edge, a)
                    self.set(x, y + level, z, ref)
            if end_ref is not None:
                for edge_a in (xa, xb) if along_x else (za, zb):
                    for b in range(lo + 1, hi):
                        x, z = (edge_a, b) if along_x else (b, edge_a)
                        self.set(x, y + level, z, end_ref)
            level += 1
        return self

    def hip_roof(self, x1, z1, x2, z2, y, ref, overhang=1):
        """Четырёхскатная крыша: вершина сходится к коньку или точке."""
        xa, xb = sorted((_r(x1), _r(x2)))
        za, zb = sorted((_r(z1), _r(z2)))
        oh = int(overhang)
        level = 0
        while xa - oh + level <= xb + oh - level and za - oh + level <= zb + oh - level:
            self.walls(xa - oh + level, y + level, za - oh + level,
                       xb + oh - level, y + level, zb + oh - level, ref)
            level += 1
        return self

    # --- движение и опоры ------------------------------------------------------------------
    def stairs(self, x, y, z, direction, length, ref, width=1):
        """Лестница из целых блоков: каждый шаг на блок вперёд и на блок вверх.
        direction: '+x', '-x', '+z' или '-z'."""
        dx, dz = {"+x": (1, 0), "-x": (-1, 0), "+z": (0, 1), "-z": (0, -1)}[direction]
        for i in range(int(length)):
            for w in range(int(width)):
                px = x + dx * i + (dz != 0) * w
                pz = z + dz * i + (dx != 0) * w
                self.box(px, y, pz, px, y + i, pz, ref)
        return self

    def columns(self, points, y1, y2, ref):
        """Колонны в списке точек [(x, z), ...] от y1 до y2."""
        for x, z in points:
            self.box(x, y1, z, x, y2, z, ref)
        return self

    def arch(self, x, y, z, radius, axis, ref, thick=1):
        """Арка-полукольцо в вертикальной плоскости: axis 'x' (арка идёт вдоль x) или 'z'."""
        for i in range(-int(radius) - 1, int(radius) + 2):
            for j in range(0, int(radius) + 2):
                d = math.hypot(i, j)
                if d <= radius + 0.3 and d >= radius + 0.3 - thick - 0.4:
                    if axis == "x":
                        self.set(x + i, y + j, z, ref)
                    else:
                        self.set(x, y + j, z + i, ref)
        return self

    def crenellations(self, x1, z1, x2, z2, y, ref):
        """Зубцы по краю площадки (башни, стены): через один блок по периметру."""
        xa, xb = sorted((_r(x1), _r(x2)))
        za, zb = sorted((_r(z1), _r(z2)))
        for x in range(xa, xb + 1):
            for z in range(za, zb + 1):
                if (x in (xa, xb) or z in (za, zb)) and (x + z) % 2 == 0:
                    self.set(x, y, z, ref)
        return self

    # --- составление -----------------------------------------------------------------------
    def bounds(self):
        if not self.cells:
            return (0, 0, 0, 0, 0, 0)
        xs, ys, zs = zip(*self.cells)
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def mirror(self, axis="x"):
        """Симметрия: добавить зеркальную копию относительно середины габарита ('x' или 'z')."""
        x1, _, z1, x2, _, z2 = self.bounds()
        for (x, y, z), name in list(self.cells.items()):
            if axis == "x":
                self.cells[(x1 + x2 - x, y, z)] = name
            else:
                self.cells[(x, y, z1 + z2 - z)] = name
        return self

    def rotated(self, degrees):
        """Новая схема, повёрнутая на 0/90/180/270 градусов вокруг вертикали."""
        out = Blueprint(self.name, self.palette)
        cells = dict(self.cells)
        for _ in range((int(degrees) % 360) // 90):
            _, _, z1, _, _, z2 = Blueprint.bounds(type("B", (), {"cells": cells})())
            depth = z2 - z1
            cells = {(depth - (z - z1), y, x): n for (x, y, z), n in cells.items()}
        out.cells = cells
        return out

    def stamp(self, other, ox=0, oy=0, oz=0, rotate=0):
        """Вставить другую схему со сдвигом (и поворотом): составные постройки."""
        source = other.rotated(rotate) if rotate else other
        x1, y1, z1, _, _, _ = source.bounds()
        for (x, y, z), name in source.cells.items():
            self.cells[(x - x1 + _r(ox), y - y1 + _r(oy), z - z1 + _r(oz))] = name
        return self

    # --- вывод -----------------------------------------------------------------------------
    def counts(self):
        out = {}
        for name in self.cells.values():
            out[name] = out.get(name, 0) + 1
        return dict(sorted(out.items(), key=lambda item: -item[1]))

    def layer_types(self):
        layers = {}
        x1, y1, z1, x2, y2, z2 = self.bounds()
        for (x, y, z), name in self.cells.items():
            layers.setdefault(y - y1, set()).add(name)
        return layers

    def stages(self, max_types=MAX_TYPES):
        """Разбиение по слоям на этапы так, чтобы в каждом было не больше max_types видов блоков.
        -> [{"layers": "0-2", "types": [...]}, ...]; слой с большим числом видов даёт ошибку."""
        stages, current, types, start = [], [], set(), 0
        for level, kinds in sorted(self.layer_types().items()):
            if len(kinds) > max_types:
                raise ValueError(f"в слое {level} {len(kinds)} видов блоков (максимум {max_types}): "
                                 "сократи палитру этого слоя")
            if current and len(types | kinds) > max_types:
                stages.append({"layers": f"{start}-{current[-1]}", "types": sorted(types)})
                current, types, start = [], set(), level
            if not current:
                start = level
            current.append(level)
            types |= kinds
        if current:
            stages.append({"layers": f"{start}-{current[-1]}", "types": sorted(types)})
        return stages

    def warnings(self):
        out = []
        names = set(self.cells.values())
        for name in sorted(names):
            if any(word in name for word in NEEDS_STATE):
                out.append(f"{name}: блок без направления встанет в положении по умолчанию "
                           "(двери, лестницы, таблички и т. п. лучше поставить вручную или заменить)")
            if any(word in name for word in GRAVITY):
                out.append(f"{name}: падает без опоры снизу")
        x1, y1, z1, x2, y2, z2 = self.bounds()
        if (x2 - x1 + 1) * (z2 - z1 + 1) * (y2 - y1 + 1) > 60 * 60 * 40:
            out.append("очень большая постройка: строй частями")
        return out

    def report(self):
        x1, y1, z1, x2, y2, z2 = self.bounds()
        lines = [f"{self.name}: {x2 - x1 + 1} x {z2 - z1 + 1} (восток x юг), высота {y2 - y1 + 1}, "
                 f"блоков {len(self.cells)}"]
        lines += [f"  {n}: {c}" for n, c in self.counts().items()]
        try:
            stages = self.stages()
            lines.append("этапы (--layers для build_plan.py): " + "; ".join(
                f"слои {s['layers']}: {', '.join(t.replace('minecraft:', '') for t in s['types'])}"
                for s in stages))
        except ValueError as error:
            lines.append("ОШИБКА ЭТАПОВ: " + str(error))
        lines += ["ВНИМАНИЕ: " + w for w in self.warnings()]
        return "\n".join(lines)

    def _legend(self):
        symbols = {}
        for name in self.counts():
            symbols[name] = SYMBOLS[len(symbols)] if len(symbols) < len(SYMBOLS) else None
            if symbols[name] is None:
                raise ValueError("слишком много разных блоков для легенды")
        return symbols

    def plan_text(self):
        """Текст схемы в формате build_plan.py (легенда и слои снизу вверх)."""
        if not self.cells:
            raise ValueError("схема пуста")
        x1, y1, z1, x2, y2, z2 = self.bounds()
        symbols = self._legend()
        out = [f"name: {self.name}", "легенда:"]
        out += [f"{symbol} = {name}" for name, symbol in symbols.items()]
        out.append(". = пропуск")
        for y in range(y1, y2 + 1):
            layer = [[symbols.get(self.cells.get((x, y, z)), ".") if (x, y, z) in self.cells else "."
                      for x in range(x1, x2 + 1)] for z in range(z1, z2 + 1)]
            if not any(ch != "." for row in layer for ch in row):
                continue
            out += ["", f"слой {y - y1}"] + ["".join(row) for row in layer]
        return "\n".join(out) + "\n"

    def preview(self, level=None):
        """Слои текстом: то же, что увидит build_plan.py preview (быстрая проверка формы)."""
        text = self.plan_text()
        if level is None:
            return text
        parts = text.split("\n\n")
        return "\n\n".join(p for p in parts if p.startswith(f"слой {level}") or "легенда" in p)

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(self.plan_text())
        return path


# --- консоль ---------------------------------------------------------------------------------
def _pairs(items):
    out = {}
    for item in items or []:
        key, _, value = item.partition("=")
        if _ == "":
            raise SystemExit(f"ожидалось ключ=значение, получено: {item}")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def main(argv=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import recipes  # noqa: E402

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("recipes")
    one = sub.add_parser("recipe")
    one.add_argument("name")
    one.add_argument("--opt", action="append", help="параметр рецепта, например w=11")
    one.add_argument("--palette", action="append", help="роль=блок, например wall=stone_bricks")
    one.add_argument("--out", help="куда записать схему (например /workspace/plans/x.txt)")
    show = sub.add_parser("show")
    show.add_argument("path")
    args = parser.parse_args(argv)
    if args.cmd == "recipes":
        for name, info in recipes.RECIPES.items():
            print(f"{name}: {info['about']}  параметры: {info['params']}  роли: {info['roles']}")
        return 0
    if args.cmd == "show":
        print(open(args.path, encoding="utf-8").read())
        return 0
    if args.name not in recipes.RECIPES:
        raise SystemExit("нет такого рецепта; смотри: python3 blueprint.py recipes")
    blueprint = recipes.RECIPES[args.name]["make"](palette=_pairs(args.palette), **_pairs(args.opt))
    print(blueprint.report())
    if args.out:
        print("записано:", blueprint.save(args.out))
    else:
        print(blueprint.preview())
    return 0


if __name__ == "__main__":
    sys.exit(main())
