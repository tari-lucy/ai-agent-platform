#!/usr/bin/env python3
"""Снимок кадра игры в папку картинок (BUILD_KIT_SHOTS).

Печатает: OK <путь> <ширина>x<высота> <байт>.

    python3 shot.py [имя] [--width 960] [--hud]

По умолчанию прячет интерфейс (чат, ники, координаты) на время снимка и возвращает его.
Нужен takeScreenshot в моде.
"""

import argparse
import base64
import json
import os
import re
import sys
import time

import kit_env
from mcp_client import McpClient, McpError


def safe_name(raw: str) -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "_", raw.lower().removesuffix(".jpg")).strip("_.-")[:80]
    return text or time.strftime("shot-%Y%m%d-%H%M%S")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("name", nargs="?", default="")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--hud", action="store_true", help="не прятать интерфейс")
    args = parser.parse_args(argv)
    name = safe_name(args.name)
    client = McpClient()
    hidden = False
    try:
        if not args.hud:
            _, error = client.call("setHudHidden", {"hidden": True})
            hidden = not error
            time.sleep(0.4)
        text, error = client.call("takeScreenshot", {"maxWidth": args.width})
        if error:
            print("ОШИБКА", text[:200])
            return 1
        data = json.loads(text)
        folder = kit_env.shots()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ".jpg")
        with open(path, "wb") as stream:
            stream.write(base64.b64decode(data["base64"]))
        print(f"OK {path} {data.get('width')}x{data.get('height')} {os.path.getsize(path)} bytes")
        return 0
    except (McpError, KeyError, ValueError) as error:
        print("ОШИБКА", error)
        return 1
    finally:
        if hidden:
            try:
                client.call("setHudHidden", {"hidden": False})
            except McpError:
                pass


if __name__ == "__main__":
    sys.exit(main())
