#!/usr/bin/env python3
"""build_plan.py: построить по текстовой схеме (слои ASCII), с проверкой.

    python3 /workspace/skills/minecraft/build_plan.py preview plan.txt [--rotate 90]
    python3 /workspace/skills/minecraft/build_plan.py check   plan.txt --origin X,Y,Z
    python3 /workspace/skills/minecraft/build_plan.py build   plan.txt --origin X,Y,Z [--layers 0-2]
    python3 /workspace/skills/minecraft/build_plan.py verify  plan.txt --origin X,Y,Z
    python3 /workspace/skills/minecraft/build_plan.py stock   plan.txt --origin X,Y,Z   (выдать недостающие материалы)
    python3 /workspace/skills/minecraft/build_plan.py build   plan.txt --origin X,Y,Z --mode commands
    python3 /workspace/skills/minecraft/build_plan.py lint    plan.txt                    (форма: висящие блоки, асимметрия, дыры...)
    python3 /workspace/skills/minecraft/build_plan.py picture plan.txt                    (картинка чертежа с четырёх сторон, ДО стройки)
    python3 /workspace/skills/minecraft/build_plan.py cleanup plan.txt --origin X,Y,Z     (убрать леса, сверить, достроить)
    python3 /workspace/skills/minecraft/build_plan.py review  plan.txt --origin X,Y,Z     (четыре кадра готовой постройки)

Порядок, чтобы вышло красиво: lint -> picture (посмотри через platform_look, поправь схему) -> check ->
build (сам уберёт леса и сверит) -> review (посмотри кадры, исправь замечания вырезами и досыпкой).

Режимы стройки (--mode):
  physical (по умолчанию)  ходит и ставит блоки руками, как игрок; работает на любом сервере, где можно
                           строить. Материал берётся из хотбара, а если его там нет, стек сам
                           перекладывается из рюкзака в хотбар. Перед стартом build сверяется с
                           getHotbar: чего нет у тебя на руках, строить не начнёт.
  commands                 /fill и /setblock через sendGameCommand: мгновенно, без хождения и материалов.
                           Только там, где у тебя есть права на команды (свой творческий мир с читами,
                           сервер, где ты оператор). Сама проверит это пробой на первой клетке.

Формат схемы (текстовый файл):

    name: станция-событие
    легенда:
    # = stone_bricks
    P = oak_planks
    . = пропуск          (клетка не трогается)
    слой 0
    #####
    #####
    слой 1
    P...P

Легенда: один символ, знак равно, имя блока (minecraft: можно не писать). Слой = этаж снизу вверх
(слой 0 на уровне Y начала), строки идут на юг (+Z), столбцы на восток (+X). Слой заканчивается
пустой строкой или следующим «слой N». Начало (--origin) = ближний к западу-северу нижний угол.
Простые блоки без состояний (без направлений и т.п.); таблички пиши руками.

Материалы берутся из ХОТБАРА (9 ячеек). `check` покажет, что положить; если видов блоков больше
девяти, режь стройку по слоям (--layers 0-1) и меняй хотбар между этапами.
"""
import argparse
import json
import math
import os
import sys
import time

SKIP = {".", " "}
SKIP_NAMES = {"пропуск", "skip", "air", "воздух"}
MAX_TYPES = 9
BATCH = 60


class PlanError(ValueError):
    pass


def norm(name):
    name = name.strip()
    return name if ":" in name else "minecraft:" + name


def parse(text):
    """-> (title, [(dx, dy, dz, block)]) with dx east, dy up, dz south from the plan's corner."""
    legend, layers, title = {}, {}, ""
    current = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        low = stripped.casefold()
        if low.startswith(("name:", "имя:")):
            title = stripped.split(":", 1)[1].strip()
            continue
        if low in ("легенда:", "legend:"):
            current = None
            continue
        if low.startswith(("слой ", "layer ")):
            try:
                current = int(stripped.split()[1])
            except (IndexError, ValueError):
                raise PlanError(f"строка {number}: не понял номер слоя") from None
            layers.setdefault(current, [])
            continue
        if stripped.startswith("#") and current is None and "=" not in stripped:
            continue  # a comment line before the layers
        if current is None:
            if "=" in stripped:
                key, name = stripped.split("=", 1)
                key = key.strip()
                if len(key) != 1:
                    raise PlanError(f"строка {number}: в легенде нужен один символ слева от =")
                legend[key] = name.split("#")[0].strip() if key != "#" else name.strip()
            continue
        if not stripped:
            current = None
            continue
        layers[current].append(line)
    blocks = []
    width = max((len(r) for rows in layers.values() for r in rows), default=0)
    for y, rows in sorted(layers.items()):
        for z, row in enumerate(rows):
            for x, ch in enumerate(row.ljust(width)):
                if ch in SKIP:
                    continue
                name = legend.get(ch)
                if name is None:
                    raise PlanError(f"слой {y}, строка {z + 1}: символа «{ch}» нет в легенде")
                if name.split()[0].casefold() in SKIP_NAMES:
                    continue
                blocks.append((x, y, z, norm(name.split()[0])))
    if not blocks:
        raise PlanError("в схеме нет блоков")
    return title, blocks


def rotate(blocks, degrees):
    degrees %= 360
    if degrees not in (0, 90, 180, 270):
        raise PlanError("поворот: 0, 90, 180 или 270")
    out = blocks
    for _ in range(degrees // 90):
        depth = max(z for _, _, z, _ in out)
        out = [(depth - z, y, x, b) for x, y, z, b in out]
    return out


def place(blocks, origin):
    ox, oy, oz = origin
    return [(ox + x, oy + y, oz + z, b) for x, y, z, b in blocks]


def parse_range(text, top):
    if not text:
        return 0, top
    if "-" in text:
        a, b = text.split("-", 1)
        return int(a), int(b)
    return int(text), int(text)


def materials(blocks):
    counts = {}
    for _, _, _, b in blocks:
        counts[b] = counts.get(b, 0) + 1
    return counts


def make_client():
    sys.path.insert(0, "/workspace/projects/unionclef")
    import mcp_client as m

    host = os.environ.get("AW_HOST_MINECRAFT_UNIONCLEF_MCP")
    url = f"http://{host}/mcp" if host else m.DEFAULT_URL
    return m.McpClient(url, m.DEFAULT_TOKEN)


def call(c, name, args=None):
    try:
        text, is_error = c.call(name, args or {})
    except Exception:  # noqa: BLE001
        return None
    if is_error:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def block_at(c, x, y, z):
    got = call(c, "getBlockAt", {"x": x, "y": y, "z": z})
    if isinstance(got, dict):
        return str(got.get("id") or got.get("name") or got.get("block") or "")
    return ""


def in_world(c):
    """The game must be in a world: a read that fails because there is none must never look like a missing block."""
    state = call(c, "getGameState")
    if isinstance(state, dict) and state.get("inGame"):
        return True
    print("Игра не в мире (или мод не отвечает): зайди в мир и повтори. Ничего не менял.")
    return False


def wait_queue(c, timeout, sleep=time.sleep, clock=time.monotonic):
    """Poll buildQueue until it is done; returns the last status."""
    start, status = clock(), {}
    while clock() - start < timeout:
        sleep(2.0)
        status = call(c, "buildQueue") or {}
        if status.get("done") or (not status.get("queued") and not status.get("remaining")):
            return status
    return status


def verify(c, blocks):
    missing = []
    for x, y, z, b in blocks:
        have = block_at(c, x, y, z)
        if have.split(":")[-1] != b.split(":")[-1]:
            missing.append((x, y, z, b))
    return missing


def have_counts(listing):
    """What the client really holds: hotbar plus the main inventory, by item id."""
    counts = {}
    for slot in listing.get("hotbar") or []:
        if slot.get("item") and slot["item"] != "empty":
            counts[slot["item"]] = counts.get(slot["item"], 0) + int(slot.get("count") or 0)
    for item, n in (listing.get("pack") or {}).items():
        counts[item] = counts.get(item, 0) + int(n)
    return counts


def missing_materials(c, blocks):
    """{block: how many are lacking}, or None when the mod cannot tell (an old mod without getHotbar)."""
    listing = call(c, "getHotbar")
    if not isinstance(listing, dict) or not listing.get("ok"):
        return None
    have = have_counts(listing)
    return {b: n - have.get(norm(b), 0) for b, n in materials(blocks).items() if have.get(norm(b), 0) < n}


def cmd_stock(c, blocks):
    """Hand out what is lacking with /give (needs command rights), then re-read what the client sees."""
    lack = missing_materials(c, blocks)
    if lack is None:
        print("Мод без getHotbar: проверить материалы нечем. Попроси оператора обновить мод.")
        return 3
    if not lack:
        print("Все материалы уже у тебя на руках.")
        return 0
    for b, n in lack.items():
        reply = call(c, "sendGameCommand", {"command": f"give @s {norm(b)} {n}"})
        if not isinstance(reply, dict) or not reply.get("ok"):
            print(f"Выдать не удалось ({b}): {reply}. Команд нет; материал надо добыть или попросить у оператора.")
            return 6
    time.sleep(2.0)
    lack = missing_materials(c, blocks)
    if lack:
        print("Выдача отправлена, но клиент не видит: " + ", ".join(f"{b.split(':')[-1]} -{n}" for b, n in lack.items()))
        print("Скорее всего, нет прав на команды. Не строй вслепую.")
        return 6
    print("Материалы выданы и видны клиенту.")
    return 0


def boxes(blocks):
    """Greedy merge of cells into boxes of one block: [(x1,y1,z1,x2,y2,z2,block)]."""
    cells = {(x, y, z): b for x, y, z, b in blocks}
    seen, out = set(), []
    for x, y, z in sorted(cells, key=lambda p: (p[1], p[2], p[0])):
        if (x, y, z) in seen:
            continue
        b = cells[(x, y, z)]
        x2 = x
        while cells.get((x2 + 1, y, z)) == b and (x2 + 1, y, z) not in seen:
            x2 += 1
        z2 = z
        while all(cells.get((xx, y, z2 + 1)) == b and (xx, y, z2 + 1) not in seen for xx in range(x, x2 + 1)):
            z2 += 1
        y2 = y
        while all(cells.get((xx, y2 + 1, zz)) == b and (xx, y2 + 1, zz) not in seen
                  for xx in range(x, x2 + 1) for zz in range(z, z2 + 1)):
            y2 += 1
        for xx in range(x, x2 + 1):
            for yy in range(y, y2 + 1):
                for zz in range(z, z2 + 1):
                    seen.add((xx, yy, zz))
        out.append((x, y, z, x2, y2, z2, b))
    return out


def cmd_commands(c, blocks):
    """Build with /fill and /setblock. Proves the rights on the first cell before sending the rest."""
    plan = boxes(sorted(blocks, key=lambda b: (b[1], b[2], b[0])))
    print(f"Команд: {len(plan)} (из {len(blocks)} блоков).")
    for index, (x1, y1, z1, x2, y2, z2, b) in enumerate(plan):
        if (x1, y1, z1) == (x2, y2, z2):
            text = f"setblock {x1} {y1} {z1} {norm(b)}"
        else:
            text = f"fill {x1} {y1} {z1} {x2} {y2} {z2} {norm(b)}"
        reply = call(c, "sendGameCommand", {"command": text})
        if not isinstance(reply, dict) or not reply.get("ok"):
            print(f"sendGameCommand отказал: {reply}. Нужна физическая стройка (--mode physical).")
            return 7
        if index == 0:
            time.sleep(1.5)
            if block_at(c, x1, y1, z1).split(":")[-1] != b.split(":")[-1]:
                print("Первая команда не сработала: нет прав на команды. Нужна физическая стройка (--mode physical).")
                return 7
        else:
            time.sleep(0.15)
    time.sleep(1.5)
    missing = verify(c, blocks)
    if missing:
        print(f"НЕ ХВАТАЕТ {len(missing)} блоков (первые): {missing[:8]}")
        return 5
    print(f"ГОТОВО: все {len(blocks)} блоков на месте (командами)")
    return 0


def design():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import design_check
    return design_check


def show_lint(blocks):
    """Print the design findings; True when there are none that stop the build."""
    findings = design().lint({(x, y, z): b for x, y, z, b in blocks})
    for level, text in findings:
        print(f"[{level}] {text}")
    if not findings:
        print("Форма: замечаний нет.")
    return not any(level == "ошибка" for level, _ in findings)


SHOTS = "/workspace/shots"
PICTURE_QUESTION = (
    "Это Minecraft-постройка. Найди недостатки: дыры, лишние или висящие блоки, асимметрия, неровная или "
    "слишком тяжёлая крыша, торчащие леса (светлые доски или столбики рядом), блоки не того материала. "
    "Перечисли конкретно, где и что исправить."
)


def cmd_picture(blocks, name):
    os.makedirs(SHOTS, exist_ok=True)
    file = f"{name}.png"
    width, height = design().save_views({(x, y, z): b for x, y, z, b in blocks}, os.path.join(SHOTS, file))
    print(f"OK {SHOTS}/{file} {width}x{height}")
    print("На листе четыре стороны: слева сверху юго-восток, справа сверху юго-запад,")
    print("слева снизу северо-запад, справа снизу северо-восток.")
    print(f"Посмотри: platform_look folder=shots photo={file} question=«{PICTURE_QUESTION}»")
    return 0


def mine_wait(c, timeout):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        time.sleep(2.0)
        status = call(c, "mineStatus") or {}
        if not status.get("mining") and not status.get("remaining"):
            return True
    return False


def cmd_cleanup(c, blocks, timeout):
    """Mine out the scaffolding the builder left (it records every block it pillared or bridged with),
    then compare with the plan and place what the cleanup took from it."""
    call(c, "ExecuteCommand", {"cmd": "@set allowBreak true"})
    try:
        reply = call(c, "cleanupScaffold") or {}
        queued = reply.get("queued", 0)
        if queued:
            print(f"Леса: убираю {queued} блоков...")
            if not mine_wait(c, timeout):
                print("Леса убраны не полностью (не дошла или время вышло): повтори cleanup.")
        else:
            print("Лесов в памяти мода нет (уже убраны или это другой запуск игры).")
    finally:
        call(c, "ExecuteCommand", {"cmd": "@set allowBreak false"})
    missing = verify(c, blocks)
    if missing:
        print(f"После уборки не хватает {len(missing)} блоков плана, достраиваю.")
        call(c, "buildBlocks", {"blocks": json.dumps([list(b) for b in missing][:BATCH])})
        wait_queue(c, timeout)
        missing = verify(c, missing)
    if missing:
        print(f"НЕ ХВАТАЕТ {len(missing)} блоков (первые): {missing[:8]}")
        return 5
    print("Постройка чистая и совпадает с планом.")
    return 0


def ground_y(c, x, z, top):
    """First standable height at (x, z), scanning down from top."""
    for y in range(top, top - 14, -1):
        here, below = call(c, "getBlockAt", {"x": x, "y": y, "z": z}), call(c, "getBlockAt", {"x": x, "y": y - 1, "z": z})
        if (isinstance(here, dict) and isinstance(below, dict)
                and str(here.get("replaceable")) == "true" and str(below.get("replaceable")) == "false"):
            return y
    return top - 13


def cmd_review(c, blocks, name):
    """Four frames of the finished build from its four corners; she looks at them and fixes what she sees."""
    here = os.path.dirname(os.path.abspath(__file__))
    xs, ys, zs = [b[0] for b in blocks], [b[1] for b in blocks], [b[2] for b in blocks]
    cx, cz = (min(xs) + max(xs)) // 2, (min(zs) + max(zs)) // 2
    reach = max(max(xs) - min(xs), max(zs) - min(zs)) // 2 + 9
    look_y = (min(ys) + max(ys)) // 2
    import subprocess
    files = []
    for index, (label, dx, dz) in enumerate((("юго-восток", 1, 1), ("юго-запад", -1, 1), ("северо-запад", -1, -1), ("северо-восток", 1, -1)), 1):
        x, z = cx + dx * reach, cz + dz * reach
        y = ground_y(c, x, z, max(ys) + 2)
        done = subprocess.run([sys.executable, os.path.join(here, "goto_wait.py"), str(x), str(y), str(z),
                               "--tolerance", "3", "--timeout", "60"], capture_output=True, text=True).stdout.strip()
        call(c, "lookAt", {"x": cx, "y": look_y, "z": cz})
        time.sleep(0.6)
        shot = f"{name}-{index}"
        out = subprocess.run([sys.executable, os.path.join(here, "shot.py"), shot], capture_output=True, text=True).stdout.strip()
        print(f"{label}: {done or 'нет ответа'} | {out}")
        if out.startswith("OK"):
            files.append((label, shot + ".jpg"))
    for label, file in files:
        print(f"Посмотри ({label}): platform_look folder=shots photo={file} question=«{PICTURE_QUESTION}»")
    return 0 if files else 9


def show_materials(blocks):
    counts = materials(blocks)
    kinds = sorted(counts.items(), key=lambda kv: -kv[1])
    print(f"Блоков: {len(blocks)}, видов: {len(kinds)}.")
    print("В хотбар (9 ячеек), по ~стаку на вид:")
    for b, n in kinds:
        print(f"  {b.split(':')[-1]} x{n}")
    if len(kinds) > MAX_TYPES:
        print(f"ВНИМАНИЕ: видов больше {MAX_TYPES}. Режь по слоям (--layers 0-1) и меняй хотбар.")
        return False
    minutes = len(blocks) * 4 / 20 / 60
    print(f"Время стройки около {max(1, round(minutes))} мин (один блок за 4 тика).")
    return True


def cmd_preview(blocks):
    height = max(y for _, y, _, _ in blocks)
    width = max(x for x, *_ in blocks) + 1
    depth = max(z for _, _, z, _ in blocks) + 1
    keys = {}
    for _, _, _, b in blocks:
        keys.setdefault(b, chr(ord("A") + len(keys)))
    for y in range(height + 1):
        print(f"слой {y}:")
        grid = [["." for _ in range(width)] for _ in range(depth)]
        for x, yy, z, b in blocks:
            if yy == y:
                grid[z][x] = keys[b]
        for row in grid:
            print("  " + "".join(row))
    print("значки: " + ", ".join(f"{v}={k.split(':')[-1]}" for k, v in keys.items()))


def cmd_build(c, blocks, timeout):
    if not show_materials(blocks):
        return 2
    lack = missing_materials(c, blocks)
    if lack:
        print("НЕ ХВАТАЕТ МАТЕРИАЛОВ у тебя на руках (хотбар и рюкзак): "
              + ", ".join(f"{b.split(':')[-1]} -{n}" for b, n in lack.items()))
        print("Запусти stock (выдаст, если есть права на команды) или добудь/попроси; вслепую не строю.")
        return 6
    shown = call(c, "getOpenScreen")
    if isinstance(shown, dict) and shown.get("open"):
        if call(c, "closeOpenScreen") is None:
            print("ОТКРЫТО ОКНО: закрыть нечем, попроси оператора нажать Esc")
            return 3
    ordered = sorted(blocks, key=lambda b: (b[1], b[0], b[2]))
    for index in range(0, len(ordered), BATCH):
        part = ordered[index : index + BATCH]
        cx = sum(b[0] for b in part) / len(part)
        cz = sum(b[2] for b in part) / len(part)
        reply = call(c, "buildBlocks", {"blocks": json.dumps([list(b) for b in part])})
        if not isinstance(reply, dict) or not reply.get("ok"):
            print(f"buildBlocks отказал на порции {index // BATCH + 1}: {reply}")
            return 4
        status = wait_queue(c, timeout)
        left = status.get("deferred") or status.get("remaining")
        if left:
            call(c, "gotoXYZ", {"x": int(cx), "y": int(part[0][1]), "z": int(cz)})
            time.sleep(6)
            call(c, "buildBlocks", {"blocks": json.dumps([list(b) for b in part])})
            wait_queue(c, timeout)
        print(f"порция {index // BATCH + 1}/{math.ceil(len(ordered) / BATCH)} готова")
    missing = verify(c, ordered)
    if missing:
        call(c, "buildBlocks", {"blocks": json.dumps([list(b) for b in missing][:BATCH])})
        wait_queue(c, timeout)
        missing = verify(c, missing)
    if missing:
        print(f"НЕ ХВАТАЕТ {len(missing)} блоков (первые): {missing[:8]}")
        print("Проверь хотбар, досягаемость, чужую защиту; запусти verify, потом build ещё раз.")
        return 5
    print(f"ГОТОВО: все {len(ordered)} блоков на месте")
    return cmd_cleanup(c, ordered, timeout)


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("action", choices=("preview", "check", "build", "verify", "stock", "lint", "picture", "cleanup", "review"))
    ap.add_argument("--mode", choices=("physical", "commands"), default="physical")
    ap.add_argument("--ignore-lint", action="store_true")
    ap.add_argument("--name", default="")
    ap.add_argument("plan")
    ap.add_argument("--origin", default="")
    ap.add_argument("--rotate", type=int, default=0)
    ap.add_argument("--layers", default="")
    ap.add_argument("--timeout", type=float, default=240.0)
    a = ap.parse_args(argv)
    try:
        with open(a.plan, encoding="utf-8") as stream:
            _, blocks = parse(stream.read())
        blocks = rotate(blocks, a.rotate)
        whole = blocks  # the form is judged on the whole plan, not on the layers picked for this stage
        low, high = parse_range(a.layers, max(y for _, y, _, _ in blocks))
        blocks = [b for b in blocks if low <= b[1] <= high]
        if not blocks:
            raise PlanError("в выбранных слоях нет блоков")
    except (OSError, PlanError, ValueError) as error:
        print("Схема не годится:", error)
        return 1
    if a.action == "preview":
        cmd_preview(blocks)
        return 0
    stem = a.name or os.path.splitext(os.path.basename(a.plan))[0].replace(" ", "_")
    if a.action == "lint":
        return 0 if show_lint(whole) else 1
    if a.action == "picture":
        return cmd_picture(whole, stem + "-plan")
    if not a.origin:
        print("Нужен --origin X,Y,Z (нижний северо-западный угол)")
        return 1
    try:
        origin = tuple(int(v) for v in a.origin.split(","))
        assert len(origin) == 3
    except (ValueError, AssertionError):
        print("--origin: три целых числа через запятую")
        return 1
    blocks = place(blocks, origin)
    if a.action == "check":
        ok = show_materials(blocks)
        show_lint(whole)
        xs, ys, zs = zip(*[(b[0], b[1], b[2]) for b in blocks])
        print(f"Занимает X {min(xs)}..{max(xs)}, Y {min(ys)}..{max(ys)}, Z {min(zs)}..{max(zs)}")
        return 0 if ok else 2
    c = client or make_client()
    if not in_world(c):
        return 10
    if a.action == "verify":
        missing = verify(c, blocks)
        print("ВСЁ НА МЕСТЕ" if not missing else f"Не хватает {len(missing)}: {missing[:8]}")
        return 0 if not missing else 5
    if a.action == "stock":
        return cmd_stock(c, blocks)
    if a.action == "cleanup":
        return cmd_cleanup(c, blocks, a.timeout)
    if a.action == "review":
        return cmd_review(c, blocks, stem)
    if a.mode == "commands":
        return cmd_commands(c, blocks)
    if not show_lint(whole) and not a.ignore_lint:
        print("Форма с ошибками: исправь схему (или --ignore-lint, если так и задумано).")
        return 8
    return cmd_build(c, blocks, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
