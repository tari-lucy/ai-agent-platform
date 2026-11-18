#!/usr/bin/env python3
"""Проверка установки: отвечает ли мод и хватает ли ему инструментов для каждой команды пакета.

    python3 check_setup.py

Апстримный UnionClef не имеет части инструментов (getHeightmap, scanBox, getHotbar, sendGameCommand,
mineBlocks): нужен форк, см. MOD.md. Этот скрипт говорит, какие команды из-за этого не заработают.
"""

import json
import sys

from mcp_client import McpClient, McpError

NEEDS = {
    "build, verify (ядро)": [
        "getGameState",
        "getBlockAt",
        "buildBlocks",
        "buildQueue",
        "buildQueueClear",
        "ExecuteCommand",
        "getOpenScreen",
        "closeOpenScreen",
        "stopPathing",
    ],
    "build: проверка материалов, stock": ["getHotbar"],
    "stock и --mode commands (игровые команды)": ["sendGameCommand"],
    "site, level (карта высот)": ["getHeightmap"],
    "cleanup (уборка лесов по миру)": ["scanBox", "cleanupScaffold", "mineBlocks", "mineStatus"],
    "dig, level (копать)": ["mineBlocks", "mineStatus"],
    "review (кадры с четырёх сторон)": [
        "takeScreenshot",
        "setHudHidden",
        "lookAt",
        "gotoXYZ",
        "pathStatus",
    ],
}


def main() -> int:
    client = McpClient()
    try:
        names = {tool["name"] for tool in client.tools()}
    except McpError as error:
        print("Нет связи с модом:", error)
        print(
            "Проверь: игра запущена, мир открыт, адрес и токен в kit.env "
            "(UNIONCLEF_MCP_URL, UNIONCLEF_MCP_TOKEN)."
        )
        return 1
    print(f"Мод отвечает по {client.url}, инструментов: {len(names)}")
    try:
        text, _ = client.call("getGameState")
        state = json.loads(text)
        print(
            "В мире: да"
            if state.get("inGame")
            else "В мире: нет (зайди в мир, иначе чтение и стройка не работают)"
        )
    except (McpError, ValueError):
        print("Состояние игры прочитать не удалось")
    missing = False
    for feature, tools in NEEDS.items():
        lack = [tool for tool in tools if tool not in names]
        if lack:
            missing = True
            print(f"  НЕТ   {feature}: не хватает {', '.join(lack)}")
        else:
            print(f"  есть  {feature}")
    if missing:
        print("\nЧасти команд не заработают: поставь форк мода с этими инструментами (MOD.md).")
        return 2
    print("\nВсё на месте.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
