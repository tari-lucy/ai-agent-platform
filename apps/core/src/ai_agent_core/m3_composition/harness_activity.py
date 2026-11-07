"""What the slow mind is doing right now, as a short true line for the fast voice.

OpenCode publishes its events on ``/event`` (server-sent events). The tool steps of the main
agent are in them: which tool, started, finished. Only the kind of the step is kept ("searches
the web", "reads files"), never its arguments or output. The fast voice reads this line, so on
"well, what is it?" it can say what really goes on instead of inventing it.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

LOG = logging.getLogger(__name__)

QUIET = frozenset({"platform_speak", "platform_voice_brief", "platform_avatar_express"})
FINISHING = frozenset({"platform_speak", "platform_voice_brief"})  # she answered by herself


def phrase(tool: str, arguments: Any) -> str:
    """The kind of a step, in words for the voice (never its content)."""
    name = tool.removeprefix("platform_")
    given = json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else ""
    if tool == "bash":
        if "minecraft" in given or "25350" in given:
            return "работает в игре"
        return "выполняет команду"
    if tool in ("read", "glob", "grep", "list"):
        return "читает файлы"
    if tool in ("edit", "write", "patch"):
        return "правит файл"
    if any(word in tool for word in ("search", "fetch", "web")):
        return "ищет в интернете"
    known = {
        "look": "смотрит картинку",
        "imagine": "рисует картинку",
        "memory_recall": "вспоминает",
        "memory_save": "записывает в память",
        "persona_brief": "вспоминает, кто она",
        "transcribe": "расшифровывает запись",
        "channel_post": "пишет в канал",
        "channel_comments": "читает комментарии",
        "channel_reply": "отвечает в канале",
        "chat_read": "читает чат",
        "chat_say": "пишет в чат",
        "wake_me": "ставит будильник",
    }
    return known.get(name, "работает с инструментом")


class Activity:
    """HarnessPart: follows OpenCode's event stream and keeps the last steps."""

    def __init__(
        self,
        url: str,
        password: str,
        *,
        on_tool: Callable[[str, str], None] | None = None,
        on_idle: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url, self.password = url.rstrip("/") + "/event", password
        self.on_tool, self.on_idle, self.clock = on_tool, on_idle, clock
        self.running: dict[str, tuple[str, float]] = {}  # call id -> (what, since)
        self.recent: deque[str] = deque(maxlen=4)
        self._task: asyncio.Task[None] | None = None

    def describe(self) -> str:
        parts: list[str] = []
        if self.running:
            what, since = max(self.running.values(), key=lambda item: item[1])
            parts.append(f"сейчас {what} (уже {int(self.clock() - since)} с)")
        else:
            parts.append("сейчас пауза между шагами или не занята")
        if self.recent:
            parts.append("недавно: " + "; ".join(self.recent))
        return ", ".join(parts)

    def feed(self, event: Any) -> None:
        """One decoded event of the stream."""
        if not isinstance(event, dict):
            return
        event = event.get("payload", event)
        kind = event.get("type")
        properties = event.get("properties") or {}
        if kind == "session.idle":
            self.running.clear()
            if self.on_idle is not None:
                self.on_idle()
            return
        if kind != "message.part.updated":
            return
        part = properties.get("part") or {}
        if part.get("type") != "tool":
            return
        tool, call = str(part.get("tool", "")), str(part.get("callID", ""))
        state = part.get("state") or {}
        status = str(state.get("status", ""))
        if tool in QUIET or not tool or not call:
            if self.on_tool is not None and tool:
                self.on_tool(tool, status)
            return
        what = phrase(tool, state.get("input"))
        if status in ("pending", "running"):
            self.running.setdefault(call, (what, self.clock()))
        elif status in ("completed", "error"):
            self.running.pop(call, None)
            if not self.recent or self.recent[-1] != what:
                self.recent.append(what)
        if self.on_tool is not None:
            self.on_tool(tool, status)

    def tick(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        wait = 2.0
        while True:
            try:
                auth = httpx.BasicAuth("opencode", self.password)
                timeout = httpx.Timeout(10.0, read=None)
                async with (
                    httpx.AsyncClient(auth=auth, timeout=timeout) as client,
                    client.stream("GET", self.url) as response,
                ):
                    response.raise_for_status()
                    wait = 2.0
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            with contextlib.suppress(ValueError):
                                self.feed(json.loads(line[5:].strip()))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - not up yet (it starts later) is normal
                LOG.debug("Activity stream: %s", type(error).__name__)
            self.running.clear()  # a dropped stream tells nothing about running steps
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30.0)
