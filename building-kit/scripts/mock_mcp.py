#!/usr/bin/env python3
"""Игра-заглушка: MCP-сервер с игрушечным миром, чтобы проверить пакет без Minecraft.

    python3 mock_mcp.py [--port 25350] [--token SECRET] [--scaffold] [--hills]

Мир плоский (поверхность на высоте 64), с --hills в нём есть холм. Заглушка понимает тот же набор
инструментов, что нужен пакету: читает и меняет блоки мгновенно (без ходьбы и очереди), принимает
/give, /fill, /setblock. С --scaffold каждая стройка оставляет «леса», чтобы проверить cleanup.
Это не мод и не проверка настоящего поведения игры: только быстрый прогон всей цепочки.
Положить в kit.env: UNIONCLEF_MCP_URL=http://127.0.0.1:25350/mcp и UNIONCLEF_MCP_TOKEN=SECRET.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG = (  # a one-pixel picture: enough for the answer of takeScreenshot
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
TOOLS = [
    "getGameState",
    "getBlockAt",
    "getHotbar",
    "getHeightmap",
    "scanBox",
    "buildBlocks",
    "buildQueue",
    "buildQueueClear",
    "sendGameCommand",
    "ExecuteCommand",
    "mineBlocks",
    "mineBlock",
    "mineStatus",
    "cleanupScaffold",
    "gotoXYZ",
    "pathStatus",
    "stopPathing",
    "lookAt",
    "setHudHidden",
    "takeScreenshot",
    "getOpenScreen",
    "closeOpenScreen",
    "getRecentChat",
    "selectHotbar",
    "setCameraView",
]
REPLACEABLE = ("air", "short_grass", "tall_grass", "dandelion")


class World:
    def __init__(self, hills: bool = False, scaffold: bool = False) -> None:
        self.hills, self.scaffold_on = hills, scaffold
        self.cells: dict[tuple[int, int, int], str] = {}  # blocks put into the world
        self.removed: set[tuple[int, int, int]] = set()  # terrain that was mined out
        self.scaffold: list[tuple[int, int, int]] = []
        self.inventory: dict[str, int] = {}
        self.pos = [0.5, 65.0, 0.5]
        self.placed = 0
        self.lock = threading.Lock()

    def ground(self, x: int, z: int) -> int:
        if self.hills and 8 <= x <= 14 and 8 <= z <= 14:
            return 67
        return 64

    def block(self, x: int, y: int, z: int) -> str:
        if (x, y, z) in self.cells:
            return self.cells[(x, y, z)]
        if (x, y, z) in self.removed:
            return "minecraft:air"
        top = self.ground(x, z)
        if y > top:
            return "minecraft:air"
        if y == top:
            return "minecraft:grass_block"
        return "minecraft:dirt" if y >= top - 3 else "minecraft:stone"

    def put(self, x: int, y: int, z: int, block: str) -> None:
        block = block if ":" in block else "minecraft:" + block
        if block.endswith("air"):
            self.cells.pop((x, y, z), None)
            if y <= self.ground(x, z):
                self.removed.add((x, y, z))
            else:
                self.cells.pop((x, y, z), None)
            return
        self.removed.discard((x, y, z))
        self.cells[(x, y, z)] = block

    # ------------------------------------------------------------------ the tools

    def call(self, name: str, a: dict):
        with self.lock:
            return getattr(self, "t_" + name)(a)

    def t_getGameState(self, a):
        return {"inGame": True, "self": {"pos": ",".join(f"{v:.1f}" for v in self.pos), "hp": 20.0}}

    def t_getBlockAt(self, a):
        x, y, z = a["x"], a["y"], a["z"]
        bid = self.block(x, y, z)
        short = bid.split(":")[-1]
        return {
            "id": bid,
            "block": short,
            "position": f"{x}, {y}, {z}",
            "is_air": str(short == "air").lower(),
            "replaceable": str(short in REPLACEABLE).lower(),
        }

    def t_getHotbar(self, a):
        items = list(self.inventory.items())
        hotbar = [
            {"slot": i, "item": items[i][0], "count": items[i][1]}
            if i < len(items)
            else {"slot": i, "item": "empty", "count": 0}
            for i in range(9)
        ]
        return {"ok": True, "hotbar": hotbar, "selected": 0, "pack": dict(items[9:])}

    def t_getHeightmap(self, a):
        x1, x2 = sorted((a["x1"], a["x2"]))
        z1, z2 = sorted((a["z1"], a["z2"]))
        palette: list[str] = []

        def pal(name: str) -> int:
            if name not in palette:
                palette.append(name)
            return palette.index(name)

        top = {}
        for (x, y, z), bid in self.cells.items():
            if (
                x1 <= x <= x2
                and z1 <= z <= z2
                and y > self.ground(x, z)
                and y >= top.get((x, z), (-1,))[0]
            ):
                top[(x, z)] = (y, bid)
        ys, ids, gys, gids = [], [], [], []
        for z in range(z1, z2 + 1):
            for x in range(x1, x2 + 1):
                g = self.ground(x, z)
                y, bid = top.get((x, z), (g, "minecraft:grass_block"))
                (
                    ys.append(y),
                    ids.append(pal(bid)),
                    gys.append(g),
                    gids.append(pal("minecraft:grass_block")),
                )
        return {
            "ok": True,
            "x1": x1,
            "z1": z1,
            "width": x2 - x1 + 1,
            "depth": z2 - z1 + 1,
            "palette": palette,
            "y": ys,
            "id": ids,
            "gy": gys,
            "gid": gids,
        }

    def t_scanBox(self, a):
        x1, x2 = sorted((a["x1"], a["x2"]))
        y1, y2 = sorted((a["y1"], a["y2"]))
        z1, z2 = sorted((a["z1"], a["z2"]))
        if (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) > 60000:
            return {"ok": False, "error": "box too large: at most 60000 cells"}
        palette: list[str] = []
        flat: list[int] = []
        for z in range(z1, z2 + 1):
            for x in range(x1, x2 + 1):
                for y in range(y1, y2 + 1):
                    bid = self.block(x, y, z)
                    if bid.endswith("air"):
                        continue
                    if bid not in palette:
                        palette.append(bid)
                    flat += [x, y, z, palette.index(bid)]
        return {"ok": True, "palette": palette, "cells": flat, "unloaded": 0}

    def t_buildBlocks(self, a):
        blocks = json.loads(a["blocks"])
        for x, y, z, name in blocks:
            self.put(x, y, z, name)
        self.placed += len(blocks)
        if self.scaffold_on and blocks:
            x, y, z, _ = blocks[0]
            spot = (x - 1, y + 1, z - 1)
            self.cells[spot] = "minecraft:oak_planks"
            self.scaffold.append(spot)
        return {"ok": True, "queued": len(blocks), "already": 0}

    def t_buildQueue(self, a):
        return {
            "done": True,
            "placed": self.placed,
            "queued": 0,
            "deferred": [],
            "deferredCount": 0,
            "blockedByOwnBody": 0,
            "deferNoMaterial": 0,
            "deferProtected": 0,
        }

    def t_buildQueueClear(self, a):
        return {"ok": True, "queued": 0}

    def t_sendGameCommand(self, a):
        parts = a["command"].strip().lstrip("/").split()
        if parts[0] == "give":
            item = parts[2] if parts[2].startswith("minecraft:") else "minecraft:" + parts[2]
            self.inventory[item] = self.inventory.get(item, 0) + (
                int(parts[3]) if len(parts) > 3 else 1
            )
        elif parts[0] == "setblock":
            self.put(int(parts[1]), int(parts[2]), int(parts[3]), parts[4])
        elif parts[0] == "fill":
            x1, y1, z1, x2, y2, z2 = (int(v) for v in parts[1:7])
            for x in range(min(x1, x2), max(x1, x2) + 1):
                for y in range(min(y1, y2), max(y1, y2) + 1):
                    for z in range(min(z1, z2), max(z1, z2) + 1):
                        self.put(x, y, z, parts[7])
        return {"ok": True, "sent": "/" + " ".join(parts)}

    def t_ExecuteCommand(self, a):
        return {"ok": True}

    def t_mineBlocks(self, a):
        for x, y, z in json.loads(a["blocks"]):
            self.put(x, y, z, "minecraft:air")
        return {"ok": True}

    def t_mineBlock(self, a):
        self.put(a["x"], a["y"], a["z"], "minecraft:air")
        return {"ok": True}

    def t_mineStatus(self, a):
        return {"ok": True, "mining": False, "remaining": 0}

    def t_cleanupScaffold(self, a):
        gone = list(self.scaffold)
        for spot in gone:
            self.put(*spot, "minecraft:air")
        self.scaffold.clear()
        return {"ok": True, "queued": len(gone)}

    def t_gotoXYZ(self, a):
        self.pos = [a["x"] + 0.5, float(a["y"]), a["z"] + 0.5]
        return {"ok": True}

    def t_pathStatus(self, a):
        return {"busy": False, "arrived": True}

    def t_stopPathing(self, a):
        return {"ok": True}

    def t_lookAt(self, a):
        return {"ok": True}

    def t_setHudHidden(self, a):
        return {"ok": True}

    def t_setCameraView(self, a):
        return {"ok": True}

    def t_takeScreenshot(self, a):
        return {"base64": PNG, "width": 1, "height": 1, "bytes": len(PNG)}

    def t_getOpenScreen(self, a):
        return {"open": False}

    def t_closeOpenScreen(self, a):
        return {"ok": True}

    def t_getRecentChat(self, a):
        return []

    def t_selectHotbar(self, a):
        return {"ok": True}


def handler(world: World, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code: int, obj=None) -> None:
            body = b"" if obj is None else json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            if token and self.headers.get("Authorization") != "Bearer " + token:
                return self._send(401, {"error": "bad token"})
            message = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
            )
            if "id" not in message:
                return self._send(202)
            method, params = message.get("method"), message.get("params") or {}
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "mock-unionclef"},
                    "capabilities": {},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": n, "description": "mock", "inputSchema": {"type": "object"}}
                        for n in TOOLS
                    ]
                }
            elif method == "tools/call":
                try:
                    out = world.call(params["name"], params.get("arguments") or {})
                    result = {
                        "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]
                    }
                except AttributeError:
                    result = {
                        "isError": True,
                        "content": [{"type": "text", "text": "Unknown tool: " + params["name"]}],
                    }
            else:
                return self._send(
                    200,
                    {"jsonrpc": "2.0", "id": message["id"], "error": {"message": "unknown method"}},
                )
            self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})

    return Handler


def serve(port: int = 25350, token: str = "SECRET", world: World | None = None):
    """Start the mock server in this thread's background; returns (server, world)."""
    world = world or World()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(world, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, world


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=25350)
    parser.add_argument("--token", default="SECRET")
    parser.add_argument("--scaffold", action="store_true")
    parser.add_argument("--hills", action="store_true")
    args = parser.parse_args()
    server, _ = serve(args.port, args.token, World(args.hills, args.scaffold))
    print(
        f"Заглушка слушает http://127.0.0.1:{args.port}/mcp, токен {args.token}. Остановить: Ctrl+C"
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
