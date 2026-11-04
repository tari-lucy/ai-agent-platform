"""Twitch chat over IRC-on-WebSocket (doc37 A3): read, and say one line when told to.

The OAuth token is a secret from the explicitly loaded host environment; it is sent once in
``PASS`` and never logged or placed in an error. Removals (CLEARMSG, CLEARCHAT) are reported so
the text can be forgotten. Rate limits and moderation are the caller's job (the hub).
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from .chat_model import MAX_NAME, ChatError, ChatEvent, ChatMessage, ChatRemoved, clean

TOKEN_SETTING = "AI_PLATFORM_TWITCH_TOKEN"
NICK_SETTING = "AI_PLATFORM_TWITCH_NICK"
URL = "wss://irc-ws.chat.twitch.tv:443"
MAX_LINE = 4096
WIRE_LOG = logging.Logger("ai_agent_adapters.stream.twitch.wire")
WIRE_LOG.disabled = True
WIRE_LOG.propagate = False
CHANNEL = re.compile(r"^[A-Za-z0-9_]{3,25}$")
LINE = re.compile(
    r"^(?:@(?P<tags>\S+) )?(?::(?P<prefix>\S+) )?(?P<command>[A-Z0-9]+)(?: (?P<rest>.*))?$"
)


@dataclass(frozen=True)
class TwitchConfig:
    channel: str
    nick: str
    url: str = URL
    connect_timeout: float = 10.0
    idle_timeout: float = 600.0  # Twitch pings every ~5 minutes; silence longer is a dead link
    allow_loopback: bool = False  # tests only: a fake server on this computer

    def __post_init__(self) -> None:
        if not CHANNEL.fullmatch(self.channel) or not CHANNEL.fullmatch(self.nick):
            raise ValueError("twitch channel or nick")
        parsed = urlsplit(self.url)
        local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
        if self.url != URL and not (self.allow_loopback and local and parsed.scheme == "ws"):
            raise ValueError("twitch url")


def _tags(raw: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in (raw or "").split(";"):
        key, _, value = part.partition("=")
        result[key] = value.replace("\\s", " ").replace("\\:", ";").replace("\\\\", "\\")
    return result


class TwitchChat:
    def __init__(self, config: TwitchConfig, token: SecretStr) -> None:
        self.config, self._token = config, token
        self._socket: ClientConnection | None = None

    async def events(self) -> AsyncGenerator[ChatEvent]:
        """Messages and removals until the link ends (``ChatError`` on a failure)."""
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                socket = await connect(
                    self.config.url, max_size=MAX_LINE * 8, logger=WIRE_LOG, proxy=None
                )
        except WebSocketException, OSError, TimeoutError:
            raise ChatError("twitch:connect") from None
        self._socket = socket
        try:
            token = self._token.get_secret_value()
            await socket.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await socket.send("PASS " + (token if token.startswith("oauth:") else "oauth:" + token))
            await socket.send(f"NICK {self.config.nick}")
            await socket.send(f"JOIN #{self.config.channel.lower()}")
            while True:
                try:
                    async with asyncio.timeout(self.config.idle_timeout):
                        frame = await socket.recv()
                except TimeoutError:
                    raise ChatError("twitch:silent") from None
                for line in str(frame).split("\r\n"):
                    event = await self._line(socket, line)
                    if event is not None:
                        yield event
        except WebSocketException, OSError:
            raise ChatError("twitch:transport") from None
        finally:
            self._socket = None
            try:
                await socket.close()
            except WebSocketException, OSError:
                pass

    async def _line(self, socket: ClientConnection, line: str) -> ChatEvent | None:
        match = LINE.match(line[:MAX_LINE]) if line else None
        if match is None:
            return None
        command, rest = match["command"], match["rest"] or ""
        tags = _tags(match["tags"])
        if command == "PING":
            await socket.send("PONG " + (rest or ":tmi.twitch.tv"))
        elif command == "RECONNECT":
            raise ChatError("twitch:reconnect")
        elif command == "NOTICE" and "authentication failed" in rest.lower():
            raise ChatError("twitch:denied")
        elif command == "PRIVMSG":
            _, _, text = rest.partition(" :")
            nick = (match["prefix"] or "").partition("!")[0]
            badges = tags.get("badges", "")
            return ChatMessage(
                "twitch",
                tags.get("id", ""),
                tags.get("user-id", nick),
                clean(tags.get("display-name") or nick, MAX_NAME),
                clean(text),
                int(tags.get("tmi-sent-ts") or time.time() * 1000) / 1000,
                any(b in badges for b in ("moderator", "broadcaster")),
            )
        elif command == "CLEARMSG":
            return ChatRemoved("twitch", id=tags.get("target-msg-id", ""))
        elif command == "CLEARCHAT":
            user = tags.get("target-user-id", "")
            return ChatRemoved("twitch", author_id=user or "*")  # no user: the whole chat cleared
        return None

    async def say(self, text: str) -> None:
        socket = self._socket
        if socket is None:
            raise ChatError("twitch:closed")
        try:
            await socket.send(f"PRIVMSG #{self.config.channel.lower()} :{clean(text)}")
        except WebSocketException, OSError:
            raise ChatError("twitch:transport") from None
