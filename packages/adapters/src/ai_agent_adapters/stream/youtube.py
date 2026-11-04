"""YouTube live chat over the Data API (doc37 A3): poll, say one line, forget removals.

OAuth: the host holds a client id, a client secret and a refresh token (local ``.env`` only);
the access token is obtained and renewed here, kept in memory, and never logged or placed in an
error. The service tells how long to wait between polls and the quota is finite, so that wait is
obeyed (never below ``min_poll``). A broadcast that is not live is "no chat", not an error.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from .chat_model import MAX_NAME, ChatError, ChatEvent, ChatMessage, ChatRemoved, clean

CLIENT_ID_SETTING = "AI_PLATFORM_YOUTUBE_CLIENT_ID"
CLIENT_SECRET_SETTING = "AI_PLATFORM_YOUTUBE_CLIENT_SECRET"  # noqa: S105 - the setting's name
REFRESH_TOKEN_SETTING = "AI_PLATFORM_YOUTUBE_REFRESH_TOKEN"  # noqa: S105
API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
MAX_BODY = 2 * 1024 * 1024
LOG = logging.getLogger(__name__)

# (method, url, headers, body) -> (status, parsed JSON or None)
Fetch = Callable[[str, str, Mapping[str, str], bytes | None], Awaitable[tuple[int, Any]]]


async def urllib_fetch(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, Any]:
    def call() -> tuple[int, Any]:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                raw, status = response.read(MAX_BODY), response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(MAX_BODY), error.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None

    return await asyncio.to_thread(call)


@dataclass(frozen=True)
class YouTubeConfig:
    video_id: str
    api: str = API
    token_url: str = TOKEN_URL
    min_poll: float = 2.0
    allow_loopback: bool = False  # tests only

    def __post_init__(self) -> None:
        if not self.video_id.replace("-", "").replace("_", "").isalnum() or len(self.video_id) > 20:
            raise ValueError("youtube video id")
        for url, official in ((self.api, API), (self.token_url, TOKEN_URL)):
            parsed = urlsplit(url)
            local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
            if url != official and not (self.allow_loopback and local):
                raise ValueError("youtube url")


class YouTubeChat:
    def __init__(
        self,
        config: YouTubeConfig,
        client_id: str,
        client_secret: SecretStr,
        refresh_token: SecretStr,
        fetch: Fetch = urllib_fetch,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config, self.fetch, self.sleep = config, fetch, sleep
        self._id, self._secret, self._refresh = client_id, client_secret, refresh_token
        self._access: str | None = None
        self._chat: str | None = None

    async def _renew(self) -> None:
        form = urllib.parse.urlencode(
            {
                "client_id": self._id,
                "client_secret": self._secret.get_secret_value(),
                "refresh_token": self._refresh.get_secret_value(),
                "grant_type": "refresh_token",
            }
        ).encode()
        status, body = await self.fetch(
            "POST",
            self.config.token_url,
            {"Content-Type": "application/x-www-form-urlencoded"},
            form,
        )
        token = body.get("access_token") if isinstance(body, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise ChatError("youtube:denied")
        self._access = token

    async def _call(
        self, method: str, path: str, query: Mapping[str, str], payload: Any = None
    ) -> Any:
        """One authorised API call; a 401 renews the token once. Fixed codes on failure."""
        for attempt in (0, 1):
            if self._access is None:
                await self._renew()
            url = f"{self.config.api}/{path}?" + urllib.parse.urlencode(query)
            headers = {"Authorization": f"Bearer {self._access}"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            try:
                status, body = await self.fetch(
                    method, url, headers, json.dumps(payload).encode() if payload else None
                )
            except OSError:
                raise ChatError("youtube:transport") from None
            if status == 401 and attempt == 0:
                self._access = None
                continue
            if status == 403 and _reason(body) in ("liveChatEnded", "liveChatNotFound"):
                raise ChatError("youtube:ended")
            if status == 403 and _reason(body) in ("quotaExceeded", "rateLimitExceeded"):
                raise ChatError("youtube:quota")
            if status != 200:
                raise ChatError(f"youtube:http:{status}")
            return body
        raise ChatError("youtube:denied")

    async def _chat_id(self) -> str:
        body = await self._call(
            "GET", "videos", {"part": "liveStreamingDetails", "id": self.config.video_id}
        )
        items = body.get("items") if isinstance(body, dict) else None
        details = (items[0].get("liveStreamingDetails") or {}) if items else {}
        chat = details.get("activeLiveChatId")
        if not isinstance(chat, str) or not chat:
            raise ChatError("youtube:not-live")
        return chat

    async def events(self) -> AsyncGenerator[ChatEvent]:
        self._chat = await self._chat_id()
        token = ""
        while True:
            query = {"liveChatId": self._chat, "part": "snippet,authorDetails"}
            if token:
                query["pageToken"] = token
            body = await self._call("GET", "liveChat/messages", query)
            if not isinstance(body, dict):
                raise ChatError("youtube:protocol")
            token = str(body.get("nextPageToken") or "")
            for item in body.get("items") or []:
                event = _event(item)
                if event is not None:
                    yield event
            wait = body.get("pollingIntervalMillis")
            await self.sleep(max(self.config.min_poll, float(wait) / 1000 if wait else 5.0))

    async def say(self, text: str) -> None:
        if self._chat is None:
            raise ChatError("youtube:closed")
        snippet = {
            "liveChatId": self._chat,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": clean(text, 200)},
        }
        await self._call("POST", "liveChat/messages", {"part": "snippet"}, {"snippet": snippet})


def _reason(body: Any) -> str:
    try:
        return str(body["error"]["errors"][0]["reason"])
    except KeyError, IndexError, TypeError:
        return ""


def _event(item: Any) -> ChatEvent | None:
    if not isinstance(item, dict):
        return None
    snippet, author = item.get("snippet") or {}, item.get("authorDetails") or {}
    kind = snippet.get("type")
    if kind == "textMessageEvent":
        text = (snippet.get("textMessageDetails") or {}).get("messageText") or snippet.get(
            "displayMessage", ""
        )
        try:
            at = datetime.fromisoformat(str(snippet.get("publishedAt"))).timestamp()
        except ValueError:
            at = 0.0
        return ChatMessage(
            "youtube",
            str(item.get("id", "")),
            str(author.get("channelId", "")),
            clean(str(author.get("displayName", "")), MAX_NAME),
            clean(str(text)),
            at,
            bool(author.get("isChatModerator") or author.get("isChatOwner")),
        )
    if kind == "messageDeletedEvent":
        return ChatRemoved(
            "youtube", id=str((snippet.get("messageDeletedDetails") or {}).get("deletedMessageId"))
        )
    if kind == "userBannedEvent":
        banned = (snippet.get("userBannedDetails") or {}).get("bannedUserDetails") or {}
        return ChatRemoved("youtube", author_id=str(banned.get("channelId", "")))
    return None
