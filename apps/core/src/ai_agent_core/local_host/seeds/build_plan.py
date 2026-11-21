#!/usr/bin/env python3
"""build_plan.py: построить по текстовой схеме (слои ASCII), с проверкой.

    python3 /workspace/skills/minecraft/build_plan.py preview plan.txt [--rotate 90]
    python3 /workspace/skills/minecraft/build_plan.py check   plan.txt --origin X,Y,Z
    python3 /workspace/skills/minecraft/build_plan.py build   plan.txt --origin X,Y,Z [--layers 0-2]
    python3 /workspace/skills/minecraft/build_plan.py verify  plan.txt --origin X,Y,Z
    python3 /workspace/skills/minecraft/build_plan.py rebuild plan.txt --origin X,Y,Z [--layers 0-2]
    (клетки, которые строитель дважды не поставил, дальше пропускаются: scratch/build-stubborn.json;
     --retry-stubborn пробует их снова)
                                                  (переделать готовое: снять лишнее и не тот материал, достроить)
    python3 /workspace/skills/minecraft/build_plan.py stock   plan.txt --origin X,Y,Z   (взять недостающее из творческого меню, без /give; в выживании - /give)
    python3 /workspace/skills/minecraft/build_plan.py build   plan.txt --origin X,Y,Z --mode commands
    python3 /workspace/skills/minecraft/build_plan.py lint    plan.txt                    (форма: висящие блоки, асимметрия, дыры...)
    python3 /workspace/skills/minecraft/build_plan.py picture plan.txt                    (картинка чертежа с четырёх сторон, ДО стройки)
    python3 /workspace/skills/minecraft/build_plan.py cleanup plan.txt --origin X,Y,Z     (убрать леса, сверить, достроить)
    python3 /workspace/skills/minecraft/build_plan.py review  plan.txt --origin X,Y,Z     (четыре кадра готовой постройки)
    python3 /workspace/skills/minecraft/build_plan.py build   plan.txt --origin X,Y,Z --timelapse   (+ таймлапс: кадры и видео)
    python3 /workspace/skills/minecraft/build_plan.py timelapse имя                        (склеить кадры в видео, если не склеились)

    python3 /workspace/skills/minecraft/build_plan.py site   7x9 [--near X,Z] [--radius 24]  (найти ровную площадку под дом 7x9)
    python3 /workspace/skills/minecraft/build_plan.py level  plan.txt --origin X,Y,Z        (выровнять участок: досыпать, срезать)
    python3 /workspace/skills/minecraft/build_plan.py dig    plan.txt --origin X,Y,Z        (выкопать: легенда «. = выкопать», нора, подвал)

Площадка: site (карта высот за один вызов, ищет окно с запасом по краям: перепад, вода, лава, деревья,
расстояние) -> берёшь её --origin -> level (до высоты origin.y - 1) -> дальше обычный порядок ниже.

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
  Телепорт запрещён в обоих режимах: никаких /tp и execute ... run tp (мод их не отправит). Ходи
  ногами. Если строитель застрял, запусти тот же этап ещё раз. Если клетка не встаёт снова, отдай её
  оператору со строкой журнала, а не перескакивай.

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
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

SKIP = {".", " "}
SKIP_NAMES = {"пропуск", "skip", "air", "воздух"}
DIG_NAMES = {"выкопать", "dig", "убрать", "вынуть"}
DIG = "minecraft:dig"  # a cell to be mined out, not a block to place
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
                if name.split()[0].casefold() in DIG_NAMES:
                    blocks.append((x, y, z, DIG))
                    continue
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


STALL = 25.0     # seconds without a single block placed (and the body blocking itself) before the build counts as stuck
UNSTICKS = 3     # how many times one stage is freed before the script says it is stuck


STILL_RADIUS = 1.5  # blocks: a body that stays inside this circle for the whole stall time is standing still


def bot_xz(c):
    """Where the body is on the ground plane, or None when the mod does not say."""
    state = call(c, "getGameState")
    try:
        x, _, z = (float(v) for v in state["self"]["pos"].split(","))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return x, z


def wait_queue(c, timeout, sleep=None, clock=None, stall=STALL):
    """Poll buildQueue until it is done; returns the last status.

    A build is stuck when it places nothing for `stall` seconds and either the body keeps blocking itself
    (blockedByOwnBody climbs) or the body does not leave its spot (jumping on the spot in a pillar that
    never gets its block is this: nothing climbs, nothing is placed). The status then carries stuck=True so
    the caller can walk out of the footprint instead of waiting for ever. A body that is walking to a cell
    is never stuck, however long the walk."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic  # looked up now, so a test can replace them
    start, status = clock(), {}
    seen, changed, blocked = None, clock(), 0
    anchor = bot_xz(c)
    while clock() - start < timeout:
        sleep(2.0)
        status = call(c, "buildQueue") or {}
        if status.get("done") or (not status.get("queued") and not status.get("remaining")):
            return status
        key = (status.get("placed"), status.get("queued"))
        grew = (status.get("blockedByOwnBody") or 0) > blocked
        blocked = status.get("blockedByOwnBody") or 0
        here = bot_xz(c)
        walking = here is not None and anchor is not None and math.dist(here, anchor) > STILL_RADIUS
        if walking:
            anchor = here
        elif anchor is None:
            anchor = here
        if key != seen or walking:
            seen, changed = key, clock()
        elif (grew or here is not None) and clock() - changed >= stall:
            return dict(status, stuck=True)
    return status


def walk_to(x, y, z, timeout=60):
    """Walk to a point and wait for the arrival (goto_wait.py); the answer line, or '' when it did not run."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    done = subprocess.run([sys.executable, os.path.join(here, "goto_wait.py"), str(x), str(y), str(z),
                           "--tolerance", "3", "--timeout", str(timeout)], capture_output=True, text=True)
    return done.stdout.strip()


def free_point(c, part):
    """A standable point outside the footprint of this stage, on the side nearest to where the body is."""
    xs, ys, zs = [b[0] for b in part], [b[1] for b in part], [b[2] for b in part]
    cx, cz = (min(xs) + max(xs)) // 2, (min(zs) + max(zs)) // 2
    state = call(c, "getGameState")
    here = (state.get("self") or {}).get("pos", "") if isinstance(state, dict) else ""
    try:
        px, _, pz = (float(v) for v in here.split(","))
    except ValueError:
        px, pz = cx, cz
    margin = 3
    options = [(min(xs) - margin, cz), (max(xs) + margin, cz), (cx, min(zs) - margin), (cx, max(zs) + margin)]
    x, z = min(options, key=lambda o: (o[0] - px) ** 2 + (o[1] - pz) ** 2)
    return x, ground_y(c, x, z, max(ys) + 2), z


def unstick(c, part):
    """Walk out of the stage's footprint and drop the jammed queue; the stage is then given again."""
    x, y, z = free_point(c, part)
    call(c, "stopPathing")
    call(c, "buildQueueClear")
    print(f"Строитель встал внутри стройки: выхожу на ({x}, {y}, {z}) и начинаю этап заново.")
    walk_to(x, y, z)


# Cells the builder has already handed back twice (NOSTAND, no face) are not given to it again for a
# while: on 2026-11-19 the same three wall cells went back to the builder in stage after stage, 61
# NOSTAND lines in half an hour. They are listed instead, for a temporary support or the operator.
# skills/minecraft/build_plan.py -> scratch/build-stubborn.json two folders up (her workspace layout)
STUBBORN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scratch", "build-stubborn.json")
STUBBORN_TRIES = 2
STUBBORN_TTL = 3 * 3600  # a newer mod or a changed site deserves a fresh try: entries expire


def _cell_key(b):
    return f"{b[0]},{b[1]},{b[2]}"


def stubborn_load():
    try:
        with open(STUBBORN_FILE, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get("at", 0) < STUBBORN_TTL}


def stubborn_save(data):
    try:
        os.makedirs(os.path.dirname(STUBBORN_FILE), exist_ok=True)
        with open(STUBBORN_FILE, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
    except OSError:
        pass  # a list that cannot be saved only costs a retry; never a reason to stop building


def stubborn_split(part):
    """-> (cells to give the builder, cells it already handed back STUBBORN_TRIES times)."""
    known = stubborn_load()
    keep, skip = [], []
    for b in part:
        entry = known.get(_cell_key(b))
        (skip if entry and entry.get("tries", 0) >= STUBBORN_TRIES else keep).append(b)
    return keep, skip


def stubborn_note(part, status):
    """Count the cells the builder handed back; forget the ones that now stand."""
    deferred = set(status.get("deferred") or [])
    known = stubborn_load()
    now = time.time()
    for b in part:
        key = _cell_key(b)
        if key in deferred:
            entry = known.get(key, {"tries": 0, "block": b[3]})
            entry["tries"] = entry.get("tries", 0) + 1
            entry["at"] = now
            entry["debug"] = str(status.get("walkDebug", ""))[-160:]
            known[key] = entry
        else:
            known.pop(key, None)
    stubborn_save(known)


def build_stage(c, part, timeout):
    """Give one stage to the builder; free the body when it jams. -> (status, jammed_for_good)"""
    part, skipped = stubborn_split(part)
    if skipped:
        print(f"Пропускаю {len(skipped)} клеток: строитель уже дважды их не поставил "
              f"({', '.join(_cell_key(b) for b in skipped[:6])}{' …' if len(skipped) > 6 else ''}). "
              f"Список: {STUBBORN_FILE}. Поставь их с временной опоры или отдай оператору; "
              "build_plan.py ... --retry-stubborn пробует их снова.")
    if not part:
        return {"deferred": [_cell_key(b) for b in skipped], "skipped": len(skipped)}, False
    status = {}
    for attempt in range(UNSTICKS + 1):
        reply = call(c, "buildBlocks", {"blocks": json.dumps([list(b) for b in part])})
        if not isinstance(reply, dict) or not reply.get("ok"):
            return {"refused": reply}, False
        status = wait_queue(c, timeout)
        if not status.get("stuck"):
            stubborn_note(part, status)
            if skipped:
                status = dict(status, deferred=list(status.get("deferred") or []) + [_cell_key(b) for b in skipped])
            return status, False
        if attempt < UNSTICKS:
            unstick(c, part)
    return status, True


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


STACK = 64  # the mod cuts a stack to the item's own limit and says how many it put


def free_slots(listing):
    """Empty slots a stack can go to: the hotbar first, then the main inventory (the builder pulls from the pack
    into the hotbar itself). An old mod does not list the pack's empty slots: then only the hotbar."""
    hot = [int(s["slot"]) for s in listing.get("hotbar") or [] if s.get("item") == "empty"]
    return hot + [int(i) for i in listing.get("packEmpty") or []]


def creative_stock(c, lack):
    """Take what is lacking from the creative menu with creativeTakeBlock: no /give, no command rights, so it works
    on a server where commands are off. -> (True, '') or (False, why). Fills only empty slots."""
    listing = call(c, "getHotbar")
    if not isinstance(listing, dict) or not listing.get("ok"):
        return False, "мод не отвечает на getHotbar"
    if listing.get("creative") is False:
        return False, "не творческий режим"
    slots = free_slots(listing)
    for block, need in lack.items():
        while need > 0:
            if not slots:
                return False, f"нет свободных ячеек для {block.split(':')[-1]} (осталось {need}): освободи место в рюкзаке"
            reply = call(c, "creativeTakeBlock", {"item": norm(block), "count": min(need, STACK), "hotbarSlot": slots.pop(0)})
            if not isinstance(reply, dict) or not reply.get("ok"):
                why = reply.get("error") if isinstance(reply, dict) else "мод не знает creativeTakeBlock"
                return False, f"{block.split(':')[-1]}: {why}"
            need -= int(reply.get("count") or STACK)
    return True, ""


def cmd_stock(c, blocks):
    """Get what is lacking: the creative menu first (no commands needed), /give only when not in creative mode."""
    lack = missing_materials(c, blocks)
    if lack is None:
        print("Мод без getHotbar: проверить материалы нечем. Попроси оператора обновить мод.")
        return 3
    if not lack:
        print("Все материалы уже у тебя на руках.")
        return 0
    taken, why = creative_stock(c, lack)
    if taken:
        time.sleep(1.0)
        lack = missing_materials(c, blocks)
        if lack:
            print("Взято из творческого меню, но клиент не видит: " + ", ".join(f"{b.split(':')[-1]} -{n}" for b, n in lack.items()))
            print("Сервер мог не принять стек (запрет на предметы). Не строй вслепую.")
            return 6
        print("Материалы взяты из творческого меню (команды не нужны) и видны клиенту.")
        return 0
    if why != "не творческий режим":
        print(f"Творческое меню не помогло ({why}); пробую /give.")
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


# What is not nature: a block with one of these in its name is something a builder put there. A plan's own
# materials count too, except the natural-looking ones (a plan in oak logs must not take a forest for scaffolding).
ARTIFICIAL = ("planks", "stone_brick", "cobblestone", "glass", "wool", "glowstone", "stripped_", "_slab", "_stairs",
              "fence", "_door", "bricks", "concrete", "lantern", "torch", "bookshelf", "crafting_table", "chest",
              "barrel", "furnace", "carpet", "_sign", "ladder", "scaffolding")
NATURAL = ("dirt", "grass", "stone", "sand", "gravel", "leaves", "_log", "_wood", "flower", "fern", "water", "lava",
           "ice", "snow", "deepslate", "andesite", "granite", "diorite", "ore", "clay", "mud", "moss", "bush",
           "vine", "cactus", "kelp", "seagrass", "coral", "mushroom", "podzol", "mycelium", "terracotta")
STRAY_MARGIN = 4  # blocks around the plan's box that are looked at for what does not belong


def read_box(c, x1, y1, z1, x2, y2, z2):
    """Non-air blocks of a box as {(x, y, z): name} and the number of unloaded columns; None without scanBox."""
    cells, unloaded = {}, 0
    for z in range(z1, z2 + 1, 40):
        reply = call(c, "scanBox", {"x1": x1, "y1": y1, "z1": z, "x2": x2, "y2": y2, "z2": min(z + 39, z2)})
        if not isinstance(reply, dict) or not reply.get("ok"):
            return None
        palette, flat = reply["palette"], reply["cells"]
        for i in range(0, len(flat), 4):
            cells[(flat[i], flat[i + 1], flat[i + 2])] = palette[flat[i + 3]]
        unloaded += reply.get("unloaded", 0)
    return cells, unloaded


def is_stray(name, plan_names):
    low = name.split(":")[-1]
    if any(k in low for k in ARTIFICIAL):
        return True
    return name in plan_names and not any(k in low for k in NATURAL)


def find_strays(c, plan, margin=STRAY_MARGIN):
    """Blocks in and around the plan's box that are built things but not in the plan: scaffolding, strays.
    -> (list of (x, y, z, name), unloaded columns), or None when the mod cannot scan."""
    wanted = {(x, y, z) for x, y, z, _ in plan}
    names = {b for _, _, _, b in plan}
    xs, ys, zs = [b[0] for b in plan], [b[1] for b in plan], [b[2] for b in plan]
    got = read_box(c, min(xs) - margin, min(ys) - 1, min(zs) - margin, max(xs) + margin, max(ys) + 8, max(zs) + margin)
    if got is None:
        return None
    world, unloaded = got
    return sorted(((x, y, z, n) for (x, y, z), n in world.items()
                   if (x, y, z) not in wanted and is_stray(n, names)), key=lambda b: (-b[1], b[2], b[0])), unloaded


CLEAN_ROUNDS = 3  # sweep, check, repair: how many times round before it is said that it did not come clean


def sweep(c, plan, timeout):
    """One sweep: the mod's own record of its scaffolding, then every built block around the plan that
    is not in the PLAN. -> how many strays are still there (0 when clean or when the mod cannot scan)."""
    call(c, "ExecuteCommand", {"cmd": "@set allowBreak true"})
    try:
        reply = call(c, "cleanupScaffold") or {}
        queued = reply.get("queued", 0)
        if queued:
            print(f"Леса из памяти мода: убираю {queued} блоков...")
            if not mine_wait(c, timeout):
                print("Леса убраны не полностью (не дошла или время вышло): повтори cleanup.")
    finally:
        call(c, "ExecuteCommand", {"cmd": "@set allowBreak false"})
    found = find_strays(c, plan)
    if found is None:
        print("Мод без scanBox: лишнее по миру найти не могу (только леса из памяти мода). Обнови мод.")
        return 0
    strays, unloaded = found
    if unloaded:
        print(f"Не прогружено {unloaded} столбцов рядом с постройкой: подойди ближе, там может остаться лишнее.")
    if not strays:
        print("Лишнего вокруг постройки нет.")
        return 0
    names = sorted({n.split(":")[-1] for _, _, _, n in strays})
    print(f"Лишнее вокруг постройки: {len(strays)} блоков ({', '.join(names[:5])}), разбираю сверху вниз.")
    left = mine_cells(c, [(x, y, z) for x, y, z, _ in strays], timeout)
    again = find_strays(c, plan)
    leftover = len(again[0]) if again else len(left)
    if leftover:
        print(f"Лишнее осталось ({leftover}): подойди к нему ближе и повтори cleanup.")
    return leftover


def cmd_cleanup(c, blocks, timeout, plan=None):
    """Take away what the builder left behind, then check the plan and put back what the cleanup took.

    The sweep goes by the world, against the WHOLE plan (a layer built earlier is not scaffolding). A repair
    can leave scaffolding of its own, so after a repair the sweep is made again: sweep, check, repair, until
    the plan stands and nothing is left over (CLEAN_ROUNDS times at most)."""
    plan = plan or blocks
    leftover, missing = 0, []
    for _ in range(CLEAN_ROUNDS):
        leftover = sweep(c, plan, timeout)
        missing = verify(c, blocks)
        if not missing:
            break
        print(f"После уборки не хватает {len(missing)} блоков плана, достраиваю.")
        call(c, "buildBlocks", {"blocks": json.dumps([list(b) for b in missing][:BATCH])})
        wait_queue(c, timeout)
    else:  # the last round ended with a repair: sweep once more and look again
        leftover = sweep(c, plan, timeout)
        missing = verify(c, blocks)
    if missing:
        print(f"НЕ ХВАТАЕТ {len(missing)} блоков (первые): {missing[:8]}")
        return 5
    if leftover:
        return 13
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


def cmd_build(c, blocks, timeout, plan=None):
    if not show_materials(blocks):
        return 2
    lack = missing_materials(c, blocks)
    if lack:
        taken, _ = creative_stock(c, lack)  # in creative mode the stacks come from the menu, with no command at all
        if taken:
            time.sleep(1.0)
            lack = missing_materials(c, blocks)
    if lack:
        print("НЕ ХВАТАЕТ МАТЕРИАЛОВ у тебя на руках (хотбар и рюкзак): "
              + ", ".join(f"{b.split(':')[-1]} -{n}" for b, n in lack.items()))
        print("Запусти stock (творческое меню, а в выживании /give, если есть права) или добудь/попроси; вслепую не строю.")
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
        status, jammed = build_stage(c, part, timeout)
        if "refused" in status:
            print(f"buildBlocks отказал на порции {index // BATCH + 1}: {status['refused']}")
            return 4
        if jammed:
            print(f"Строитель застрял на порции {index // BATCH + 1}: за {UNSTICKS} попытки не поставил ни одного блока "
                  "(встаёт внутрь стройки). Для творческого мира с читами: --mode commands.")
            return 11
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
        missing = reopen_from_above(c, missing, plan or ordered, timeout)
    if missing:
        print(f"НЕ ХВАТАЕТ {len(missing)} блоков (первые): {missing[:8]}")
        print("Проверь хотбар, досягаемость, чужую защиту; запусти verify, потом build ещё раз.")
        return 5
    print(f"ГОТОВО: все {len(ordered)} блоков на месте")
    return cmd_cleanup(c, ordered, timeout, plan)


def reopen_from_above(c, missing, plan, timeout):
    """A cell skipped early and then sealed by the layer above it has no face left to click: take the
    plan block above it away for a moment, place the cell, put the block back. 2026-11-21: a base cell
    of the striped lighthouse, the builder standing right next to it and finding no face, while she
    did exactly this by hand with a separate repair plan. -> the cells still missing."""
    want = {(x, y, z): b for x, y, z, b in plan}
    left = []
    for x, y, z, b in missing:
        above = (x, y + 1, z)
        top = want.get(above)
        if top is None or block_at(c, *above).split(":")[-1] != norm(top).split(":")[-1]:
            left.append((x, y, z, b))
            continue
        print(f"Клетка {(x, y, z)} закрыта сверху: снимаю {top.split(':')[-1]} над ней, ставлю и возвращаю.")
        if mine_cells(c, [above], timeout):
            left.append((x, y, z, b))
            continue
        call(c, "buildBlocks", {"blocks": json.dumps([[x, y, z, b], [x, y + 1, z, top]])})
        wait_queue(c, timeout)
        if verify(c, [(x, y, z, b)]):
            left.append((x, y, z, b))
        if verify(c, [(x, y + 1, z, top)]):
            print(f"Блок {above} не вернулся на место: верни его (build ещё раз по этому плану).")
    return left


def terrain():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import terrain as module
    return module


def read_heights(c, x1, z1, x2, z2):
    """Heights of a rectangle, in tiles of 80x80 (what one getHeightmap call returns), joined into one map."""
    t = terrain()
    tiles = []
    for tz in range(z1, z2 + 1, 80):
        for tx in range(x1, x2 + 1, 80):
            reply = call(c, "getHeightmap", {"x1": tx, "z1": tz, "x2": min(tx + 79, x2), "z2": min(tz + 79, z2)})
            tiles.append(t.Heights.from_reply(reply))
    if len(tiles) == 1:
        return tiles[0]
    return t.merge(tiles)


def cmd_site(c, width, depth, near, radius, margin):
    """Find flat, dry, tree-free ground for a width x depth building with margin blocks of room around it."""
    t = terrain()
    if near is None:
        state = call(c, "getGameState")
        pos = (state.get("self") or {}).get("pos", "") if isinstance(state, dict) else ""
        try:
            near = tuple(int(float(v)) for v in pos.split(",")[0::2])
        except ValueError:
            print("Не знаю, где ты: укажи --near X,Z.")
            return 1
    radius = max(6, min(radius, 39))
    heights = read_heights(c, near[0] - radius, near[1] - radius, near[0] + radius, near[1] + radius)
    sites = t.find_sites(heights, width, depth, margin=margin, near=near)
    if not sites:
        print(f"Подходящей площадки {width}x{depth} (+{margin} по краям) в радиусе {radius} нет: перепад больше 4, "
              "вода, лава или чанки не прогружены. Подойди в другое место или увеличь --radius (до 39).")
        return 7
    print(f"Лучшие места под {width}x{depth} (запас {margin} блока со всех сторон), первое лучшее:")
    for index, site in enumerate(sites[:3], 1):
        print(f"{index}. {t.describe(site, width, depth)}")
    print("Пол здания (слой 0) встанет на высоте Y из --origin. Перед стройкой: build_plan.py level ... --origin ...")
    return 0


MINE_REACH = 4.3       # eye to block centre the miner works at (the mod gives up beyond 4.5)
MINE_WALK_ROUNDS = 8   # walks to a closer stand before the rest is handed back
PASS_THROUGH = ("air", "grass", "fern", "flower", "poppy", "dandelion", "torch", "vine", "sapling",
                "tulip", "bush", "carpet", "snow")


def _open(name):
    low = name.split(":")[-1]
    return low.endswith("air") or (low != "grass_block" and any(k in low for k in PASS_THROUGH))


def reach_stand(c, cell, here=None, reach=MINE_REACH, avoid=()):
    """A place to stand (air at feet and head, something solid under the feet) from which the eye is
    within ``reach`` of ``cell``; the nearest to ``here`` among the closest. None without scanBox or
    when there is none. A stand whose floor is one of ``avoid`` (cells still to be mined) is not one:
    mining it would drop her."""
    x0, y0, z0 = cell
    avoid = set(avoid)
    got = read_box(c, x0 - 5, y0 - 6, z0 - 5, x0 + 5, y0 + 5, z0 + 5)
    if got is None:
        return None
    world = got[0]
    def open_(p):
        return _open(world.get(p, "minecraft:air"))
    best, key = None, None
    for x in range(x0 - 5, x0 + 6):
        for y in range(y0 - 5, y0 + 5):
            for z in range(z0 - 5, z0 + 6):
                if not (open_((x, y, z)) and open_((x, y + 1, z)) and not open_((x, y - 1, z))):
                    continue
                if (x, y - 1, z) in avoid:
                    continue
                d = math.dist((x + 0.5, y + 1.62, z + 0.5), (x0 + 0.5, y0 + 0.5, z0 + 0.5))
                if d > reach:
                    continue
                far = math.dist((x, y, z), here) if here else 0.0
                k = (round(d), far)
                if key is None or k < key:
                    best, key = (x, y, z), k
    return best


def _here(c):
    state = call(c, "getGameState")
    pos = (state.get("self") or {}).get("pos", "") if isinstance(state, dict) else ""
    try:
        return tuple(float(v) for v in pos.split(","))
    except ValueError:
        return None


def _mine_pass(c, todo, timeout, step):
    reply = call(c, "mineBlocks", {"blocks": json.dumps([list(p) for p in todo])}) or {}
    if not reply.get("ok"):
        print(f"mineBlocks отказал: {reply}")
        return False
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        time.sleep(step)
        status = call(c, "mineStatus") or {}
        if not status.get("mining") and not status.get("remaining"):
            break
    return True


def walk_close(x, y, z, timeout=60):
    """Walk to a stand and wait (goto_wait.py, tight tolerance: the point is to be in reach)."""
    here = os.path.dirname(os.path.abspath(__file__))
    done = subprocess.run([sys.executable, os.path.join(here, "goto_wait.py"), str(x), str(y), str(z),
                           "--tolerance", "1.2", "--timeout", str(timeout)], capture_output=True, text=True)
    return done.stdout.strip()


def mine_cells(c, cells, timeout, step=1.0):
    """Mine the given cells (top-down) and wait; returns the cells that are still not air.

    The mod's miner breaks only what is in reach from where the body stands, so what is left after the
    first pass is walked to: the highest cell first, from the nearest stand with that cell in reach
    (the navigator climbs there if it must; its pillar is the next sweep's scaffolding). Measured on
    2026-11-21: a 10-high lighthouse kept 27 scaffold planks and 28 cells at y 72-75, "подойди ближе"
    every time, because nobody walked her up to them."""
    todo = [(x, y, z) for x, y, z in cells if not block_at(c, x, y, z).endswith("air")]
    if not todo:
        return []
    call(c, "ExecuteCommand", {"cmd": "@set allowBreak true"})
    try:
        if not _mine_pass(c, todo, timeout, step):
            return todo
        left = [p for p in todo if not block_at(c, *p).endswith("air")]
        for _ in range(MINE_WALK_ROUNDS):
            if not left:
                break
            target = max(left, key=lambda p: p[1])
            stand = reach_stand(c, target, _here(c), avoid=left)
            if stand is None:
                print(f"Не нашла, где встать, чтобы дотянуться до {target}: оставляю {len(left)} клеток.")
                break
            print(f"Подхожу к ({stand[0]}, {stand[1]}, {stand[2]}), чтобы достать {target} и соседние.")
            # ⛔ NEVER WALK WITH BREAKING ON (2026-11-21). allowBreak stayed true through the walk, so
            # the navigator dug its own road through the lighthouse to reach the stand. Breaking is
            # for the listed cells only: off for the walk, back on for the mining.
            call(c, "ExecuteCommand", {"cmd": "@set allowBreak false"})
            walk_close(*stand)
            call(c, "ExecuteCommand", {"cmd": "@set allowBreak true"})
            eye = (stand[0] + 0.5, stand[1] + 1.62, stand[2] + 0.5)
            near = [p for p in left if math.dist(eye, (p[0] + 0.5, p[1] + 0.5, p[2] + 0.5)) <= MINE_REACH + 0.2]
            if not _mine_pass(c, sorted(near, key=lambda p: -p[1]), timeout, step):
                break
            still = [p for p in left if not block_at(c, *p).endswith("air")]
            if len(still) >= len(left):
                print(f"С ({stand[0]}, {stand[1]}, {stand[2]}) ничего не снялось: оставляю {len(still)} клеток.")
                left = still
                break
            left = still
        return left
    finally:
        call(c, "ExecuteCommand", {"cmd": "@set allowBreak false"})


def rebuild_removals(world, plan, box_plan=None):
    """What must come off before the new plan goes up: inside the plan's box (of box_plan, the layers of this stage)
    the plan is the whole truth, so a block it does not list there, or a different block, is taken off.
    -> cells top-down, so nothing is mined out from under what is still to be mined."""
    want = {(x, y, z): norm(b).split(":")[-1] for x, y, z, b in plan}
    box = box_plan or plan
    xs, ys, zs = [b[0] for b in box], [b[1] for b in box], [b[2] for b in box]
    out = []
    for (x, y, z), name in world.items():
        if not (min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys) and min(zs) <= z <= max(zs)):
            continue
        low = name.split(":")[-1]
        if low.endswith("air"):
            continue
        if want.get((x, y, z)) != low:
            out.append((x, y, z))
    return sorted(out, key=lambda p: (-p[1], p[2], p[0]))


def cmd_rebuild(c, blocks, whole, timeout, mode):
    """Clear what the new plan does not want inside its box; the caller then builds the plan as usual.
    -> None to go on building, or an exit code."""
    xs, ys, zs = [b[0] for b in blocks], [b[1] for b in blocks], [b[2] for b in blocks]
    got = read_box(c, min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    if got is None:
        print("Мод без scanBox: не вижу, что снимать. Обнови мод.")
        return 13
    world, unloaded = got
    if unloaded:
        print(f"Не прогружено {unloaded} столбцов постройки: подойди ближе и повтори rebuild.")
        return 7
    remove = rebuild_removals(world, whole, blocks)
    print(f"Переделка: снять {len(remove)} (лишнее и не тот материал), потом достроить по схеме.")
    if not remove:
        return None
    if mode == "commands":
        code = cmd_commands(c, [(x, y, z, "minecraft:air") for x, y, z in remove])
        return code or None
    left = mine_cells(c, remove, timeout)
    if left:
        print(f"Не снято {len(left)} клеток (первые): {left[:8]}. Подойди ближе и повтори rebuild.")
        return 5
    print(f"Снято {len(remove)}. Достраиваю.")
    return None


def dig_hazards(c, cells, limit=300):
    """Water, lava or loose gravel touching the cells to be mined: a flood or a cave-in waits there."""
    cellset = set(cells)
    shell = []
    for x, y, z in cells:
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            n = (x + dx, y + dy, z + dz)
            if n not in cellset and n not in shell:
                shell.append(n)
    if len(shell) > limit:
        return None, len(shell)
    found = []
    for x, y, z in shell:
        name = block_at(c, x, y, z).split(":")[-1]
        if any(k in name for k in ("water", "lava", "sand", "gravel")):
            found.append(((x, y, z), name))
    return found, len(shell)


def cmd_dig(c, cells, mode, timeout, force=False):
    """Mine a room, a tunnel or a cellar out of the ground: from a plan whose legend says «выкопать»."""
    print(f"Выкопать: {len(cells)} клеток.")
    if mode == "commands":
        return cmd_commands(c, [(x, y, z, "minecraft:air") for x, y, z in cells])
    found, checked = dig_hazards(c, cells)
    if found is None:
        print(f"Рядом {checked} клеток: слишком много, чтобы проверить воду, лаву и песок. Копай меньшими частями.")
        return 12
    if found and not force:
        print("ОПАСНО: рядом с выработкой " + ", ".join(f"{name} {pos}" for pos, name in found[:6])
              + (f" и ещё {len(found) - 6}" if len(found) > 6 else "")
              + ". Затопит или обрушит. Сдвинь выработку или --force, если так и задумано.")
        return 12
    left = mine_cells(c, sorted(cells, key=lambda p: (-p[1], p[2], p[0])), timeout)
    if left:
        print(f"Не выкопано {len(left)} клеток (первые): {left[:8]}. Подойди ближе и повтори dig.")
        return 5
    print("ГОТОВО: всё выкопано")
    return 0


def cmd_level(c, whole, origin, mode, margin, timeout, material):
    """Make the footprint of the plan (plus margin) level at origin.y - 1: fill what is low, cut what is high."""
    t = terrain()
    ox, oy, oz = origin
    width, depth = max(b[0] for b in whole) + 1, max(b[2] for b in whole) + 1
    heights = read_heights(c, ox - margin - 1, oz - margin - 1, ox + width + margin, oz + depth + margin)
    try:
        plan = t.level_plan(heights, ox, oz, width, depth, ground=oy - 1, margin=margin, material=material)
    except ValueError as error:
        print(error)
        return 7
    fill, cut = plan["fill"], plan["cut"]
    print(f"Выровнять до высоты {oy - 1}: досыпать {len(fill)}, срезать {len(cut)} (участок {width}x{depth}, запас {margin}).")
    if not fill and not cut:
        print("Уже ровно.")
        return 0
    if mode == "commands":
        return cmd_commands(c, [(x, y, z, "minecraft:air") for x, y, z in cut] + fill)
    if cut:
        left = mine_cells(c, cut, timeout)
        if left:
            print(f"Не срезано {len(left)} клеток (первые): {left[:8]}. Подойди ближе и повтори level.")
            return 5
    if fill:
        lack = missing_materials(c, fill)
        if lack:
            print("НЕ ХВАТАЕТ материала для засыпки: " + ", ".join(f"{b.split(':')[-1]} -{n}" for b, n in lack.items())
                  + ". Запусти stock по схеме засыпки или добудь.")
            return 6
        for index in range(0, len(fill), BATCH):
            status, jammed = build_stage(c, fill[index:index + BATCH], timeout)
            if "refused" in status or jammed:
                print("Засыпка не пошла (строитель отказал или застрял). Для творческого мира: --mode commands.")
                return 11
        missing = verify(c, fill)
        if missing:
            print(f"Не досыпано {len(missing)} клеток (первые): {missing[:8]}")
            return 5
    print("ГОТОВО: участок ровный")
    return 0


# ----------------------------------------------------------------------------------- timelapse ---
# While a build runs, a background thread asks the mod for a frame every few seconds and keeps them in
# <shots>/timelapse-<name>/frame_00001.jpg ...; at the end ffmpeg (when there is one) makes an mp4 of them.

TL_EVERY = 3.0  # seconds between two frames
TL_WIDTH = 1280  # takeScreenshot maxWidth
TL_MAX = 6000  # frames kept at most: a runaway build must not fill the disk
TL_FAILS = 5  # frames missed in a row before the capture stops by itself
TL_FPS = 24


class Timelapse:
    """Frames of the game while something is built. shoot() -> JPEG bytes, or None when no frame came."""

    def __init__(self, folder, shoot, every=TL_EVERY, hide_hud=None, show_hud=None, view=None):
        self.folder, self.shoot, self.every = folder, shoot, every
        self.hide_hud, self.show_hud, self.view = hide_hud, show_hud, view
        self.frames, self.misses = 0, 0
        self._stop = threading.Event()
        self._thread = None

    def _take(self):
        try:
            data = self.shoot()
        except Exception:  # noqa: BLE001 - a missed frame is not a failed build
            data = None
        if not data:
            self.misses += 1
            return
        self.misses = 0
        self.frames += 1
        with open(os.path.join(self.folder, f"frame_{self.frames:05d}.jpg"), "wb") as stream:
            stream.write(data)

    def _run(self):
        while not self._stop.wait(self.every):
            if self.frames >= TL_MAX or self.misses >= TL_FAILS:
                return
            self._take()

    def start(self):
        os.makedirs(self.folder, exist_ok=True)
        for old in os.listdir(self.folder):  # a new recording replaces the old one of the same name
            if old.startswith("frame_") and old.endswith(".jpg"):
                os.remove(os.path.join(self.folder, old))
        if self.hide_hud:
            self.hide_hud()
        if self.view:
            self.view(self.view_number)
        self._take()  # the first frame is the empty plot
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self._take()  # the last frame is the finished build
        if self.view:
            self.view(0)
        if self.show_hud:
            self.show_hud()
        return self.frames


def stitch(folder, out, fps=TL_FPS):
    """Make an mp4 of the frames of a folder with ffmpeg. -> (path or None, what to tell)"""
    if not [f for f in os.listdir(folder) if f.startswith("frame_")]:
        return None, "кадров нет"
    command = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", os.path.join(folder, "frame_%05d.jpg"),
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", out]
    if shutil.which("ffmpeg") is None:
        return None, "ffmpeg здесь нет; склей там, где он есть: " + " ".join(command)
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0 or not os.path.isfile(out):
        return None, "ffmpeg не справился: " + (done.stderr.strip().splitlines() or ["?"])[-1][:160]
    return out, f"видео: {out}"


def timelapse_paths(name):
    folder = os.path.join(SHOTS, f"timelapse-{name}")
    return folder, os.path.join(SHOTS, f"timelapse-{name}.mp4")


def frame_shooter(c):
    """A function that asks the mod for one frame and gives back its JPEG bytes (None when none came)."""
    def shoot():
        text, error = c.call("takeScreenshot", {"maxWidth": TL_WIDTH})
        if error:
            return None
        return base64.b64decode(json.loads(text)["base64"])
    return shoot


def make_timelapse(c, name, every, hud, view):
    folder, _ = timelapse_paths(name)

    def hidden(flag):
        return lambda: call(c, "setHudHidden", {"hidden": flag})

    recorder = Timelapse(folder, frame_shooter(c), every, hidden(True) if not hud else None,
                         hidden(False) if not hud else None, (lambda number: call(c, "setCameraView", {"view": number})) if view else None)
    recorder.view_number = view
    return recorder


def finish_timelapse(recorder, name, fps=TL_FPS):
    frames = recorder.stop()
    folder, out = timelapse_paths(name)
    print(f"Таймлапс: {frames} кадров в {folder}")
    path, note = stitch(folder, out, fps)
    print("Таймлапс: " + note)
    return path


def cmd_timelapse(name, fps):
    folder, out = timelapse_paths(name)
    if not os.path.isdir(folder):
        print(f"Нет кадров: {folder}")
        return 1
    path, note = stitch(folder, out, fps)
    print(note)
    return 0 if path else 1


def main(argv=None, client=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("action", choices=("preview", "check", "build", "verify", "stock", "lint", "picture", "cleanup", "review", "site", "level", "dig", "timelapse", "rebuild"))
    ap.add_argument("--mode", choices=("physical", "commands"), default="physical")
    ap.add_argument("--ignore-lint", action="store_true")
    ap.add_argument("--name", default="")
    ap.add_argument("--near", default="")
    ap.add_argument("--radius", type=int, default=24)
    ap.add_argument("--margin", type=int, default=-1)
    ap.add_argument("--material", default="dirt")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timelapse", action="store_true", help="build: record frames and make a video")
    ap.add_argument("--timelapse-every", type=float, default=TL_EVERY, help="seconds between frames")
    ap.add_argument("--timelapse-view", type=int, choices=(0, 1, 2), default=0,
                    help="camera for the recording: 1 third person from behind, 2 from the front, 0 as it is")
    ap.add_argument("--timelapse-hud", action="store_true", help="leave the interface (chat, hotbar) in the frames")
    ap.add_argument("--fps", type=int, default=TL_FPS)
    ap.add_argument("plan")
    ap.add_argument("--origin", default="")
    ap.add_argument("--rotate", type=int, default=0)
    ap.add_argument("--layers", default="")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--retry-stubborn", action="store_true",
                    help="give the builder again the cells it handed back twice (after a new mod, a support)")
    a = ap.parse_args(argv)
    if a.retry_stubborn:
        stubborn_save({})
    if a.action == "timelapse":
        return cmd_timelapse(a.plan, a.fps)
    if a.action == "site":
        import re
        size = re.fullmatch(r"(\d+)[xх×](\d+)", a.plan.strip())
        try:
            if size:
                width, depth = int(size[1]), int(size[2])
            else:
                with open(a.plan, encoding="utf-8") as stream:
                    _, found = parse(stream.read())
                found = [b for b in found if b[3] != DIG]
                width, depth = max(b[0] for b in found) + 1, max(b[2] for b in found) + 1
            near = tuple(int(v) for v in a.near.split(",")) if a.near else None
            assert near is None or len(near) == 2
        except (OSError, PlanError, ValueError, AssertionError):
            print("site: размер как 7x9 или файл схемы; --near X,Z (два числа).")
            return 1
        c = client or make_client()
        if not in_world(c):
            return 10
        return cmd_site(c, width, depth, near, a.radius, 2 if a.margin < 0 else a.margin)
    try:
        with open(a.plan, encoding="utf-8") as stream:
            _, blocks = parse(stream.read())
        blocks = rotate(blocks, a.rotate)
        digs = [b for b in blocks if b[3] == DIG]
        blocks = [b for b in blocks if b[3] != DIG]
        if a.action == "dig":
            if not digs:
                raise PlanError("в схеме нет клеток «выкопать» (в легенде: . = выкопать)")
            blocks = digs
        elif not blocks:
            raise PlanError("в схеме нет блоков для постановки")
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
        xs, ys, zs = zip(*[(b[0], b[1], b[2]) for b in blocks], strict=True)
        print(f"Занимает X {min(xs)}..{max(xs)}, Y {min(ys)}..{max(ys)}, Z {min(zs)}..{max(zs)}")
        return 0 if ok else 2
    c = client or make_client()
    if not in_world(c):
        return 10
    if a.action == "level":
        return cmd_level(c, whole, origin, a.mode, 1 if a.margin < 0 else a.margin, a.timeout, a.material)
    if a.action == "dig":
        return cmd_dig(c, [(x, y, z) for x, y, z, _ in blocks], a.mode, a.timeout, a.force)
    if a.action == "verify":
        missing = verify(c, blocks)
        print("ВСЁ НА МЕСТЕ" if not missing else f"Не хватает {len(missing)}: {missing[:8]}")
        return 0 if not missing else 5
    if a.action == "stock":
        return cmd_stock(c, blocks)
    if a.action == "cleanup":
        everything = place(whole, origin)
        return cmd_cleanup(c, everything, a.timeout, everything)
    if a.action == "review":
        return cmd_review(c, blocks, stem)
    if a.action == "rebuild":
        code = cmd_rebuild(c, blocks, place(whole, origin), a.timeout, a.mode)
        if code is not None:
            return code
    if a.mode == "commands":
        return cmd_commands(c, blocks)
    if not show_lint(whole) and not a.ignore_lint:
        print("Форма с ошибками: исправь схему (или --ignore-lint, если так и задумано).")
        return 8
    if a.timelapse:
        recorder = make_timelapse(
            client or make_client(), stem, max(0.5, a.timelapse_every), a.timelapse_hud, a.timelapse_view
        ).start()
        try:
            return cmd_build(c, blocks, a.timeout, place(whole, origin))
        finally:
            finish_timelapse(recorder, stem, a.fps)
    return cmd_build(c, blocks, a.timeout, place(whole, origin))


if __name__ == "__main__":
    sys.exit(main())
