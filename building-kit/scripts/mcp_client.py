#!/usr/bin/env python3
"""Клиент к MCP-серверу мода UnionClef. Только стандартная библиотека.

Мод поднимает сервер сам, внутри запущенного Minecraft: http://<адрес>:25350/mcp, заголовок
Authorization: Bearer <токен> (настройки мода mcpPort и mcpAuthToken). Адрес и токен берутся из
kit.env (см. kit_env.py).

    python3 mcp_client.py ping
    python3 mcp_client.py tools
    python3 mcp_client.py call getHotbar '{}'
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

import kit_env

DEFAULT_URL = kit_env.url()
DEFAULT_TOKEN = kit_env.token()


class McpError(Exception):
    pass


class McpClient:
    """Минимальный клиент: initialize при первом вызове, call() возвращает (текст, это_ошибка)."""

    def __init__(
        self, url: str | None = None, token: str | None = None, timeout: float = 60.0
    ) -> None:
        self.url = url or DEFAULT_URL
        self.token = DEFAULT_TOKEN if token is None else token
        self.timeout = timeout
        self._id = 0
        self._ready = False
        # адрес обычно локальный или из частной сети: идти через системный прокси туда нельзя
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _rpc(self, method: str, params: dict | None = None) -> dict | None:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            hint = " (проверь токен в kit.env)" if error.code in (401, 403) else ""
            raise McpError(f"HTTP {error.code}{hint}") from error
        except OSError as error:
            raise McpError(f"нет связи с модом по {self.url}: {error}") from error
        if not raw.strip():
            return None
        if "data:" in raw and not raw.lstrip().startswith("{"):
            raw = raw.split("data:")[-1].strip()
        message = json.loads(raw)
        if "error" in message:
            raise McpError(f"{method}: {message['error'].get('message')}")
        return message.get("result")

    def initialize(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "building-kit", "version": "1"},
            },
        )
        self._ready = True

    def tools(self) -> list[dict]:
        if not self._ready:
            self.initialize()
        return (self._rpc("tools/list") or {}).get("tools", [])

    def call(self, name: str, arguments: dict | None = None) -> tuple[str, bool]:
        if not self._ready:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        return text, bool(result.get("isError"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=("ping", "tools", "call"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args(argv)
    client = McpClient()
    try:
        if args.action == "ping":
            client.initialize()
            print("OK", client.url)
        elif args.action == "tools":
            for tool in client.tools():
                print(tool["name"])
        else:
            text, error = client.call(args.name, json.loads(args.arguments))
            print(text)
            return 1 if error else 0
    except McpError as error:
        print("ОШИБКА:", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
