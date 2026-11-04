"""Twitch (IRC over a real loopback WebSocket) and YouTube (a fake HTTP layer)."""

import asyncio
import json
import logging
from contextlib import aclosing
from urllib.parse import parse_qs, urlsplit

import pytest
from ai_agent_adapters.stream.chat_model import ChatError, ChatMessage, ChatRemoved, clean
from ai_agent_adapters.stream.twitch import TwitchChat, TwitchConfig
from ai_agent_adapters.stream.youtube import YouTubeChat, YouTubeConfig
from pydantic import SecretStr
from websockets.asyncio.server import serve

QUIET = logging.Logger("fake-chat")
QUIET.disabled = True
TOKEN = "synthetic-twitch-token"


def twitch_server(sent, script):
    async def handler(ws):
        for _ in range(4):  # CAP, PASS, NICK, JOIN
            sent.append(await ws.recv())
        for frame in script:
            await ws.send(frame)
        try:
            async for raw in ws:
                sent.append(raw)
        except Exception:  # noqa: BLE001
            pass

    return handler


def twitch(server, **kwargs):
    port = server.sockets[0].getsockname()[1]
    config = TwitchConfig(
        "Opechatka", "opechatka", url=f"ws://127.0.0.1:{port}", allow_loopback=True, **kwargs
    )
    return TwitchChat(config, SecretStr(TOKEN))


def test_twitch_login_messages_removals_ping_and_say():
    script = [
        "@badges=moderator/1;display-name=Zoya;id=m1;tmi-sent-ts=1700000000000;user-id=42 "
        ":zoya!zoya@zoya.tmi.twitch.tv PRIVMSG #opechatka :Привет, \x07Опечатка!\r\n",
        "PING :tmi.twitch.tv\r\n",
        "@target-msg-id=m1 :tmi.twitch.tv CLEARMSG #opechatka :Привет\r\n",
        "@target-user-id=42 :tmi.twitch.tv CLEARCHAT #opechatka :zoya\r\n",
        ":tmi.twitch.tv CLEARCHAT #opechatka\r\n",
    ]

    async def scenario():
        sent = []
        async with serve(twitch_server(sent, script), "127.0.0.1", 0, logger=QUIET) as server:
            chat, got = twitch(server), []
            async with aclosing(chat.events()) as events:
                async for event in events:
                    got.append(event)
                    if len(got) == 4:
                        await chat.say("Здравствуй,\nмир")
                        await asyncio.sleep(0.1)
                        break
        assert got[0] == ChatMessage("twitch", "m1", "42", "Zoya", "Привет, Опечатка!", 1.7e9, True)
        assert got[1] == ChatRemoved("twitch", id="m1")
        assert got[2] == ChatRemoved("twitch", author_id="42")
        assert got[3] == ChatRemoved("twitch", author_id="*")  # the whole chat was cleared
        assert sent[1] == "PASS oauth:" + TOKEN and sent[3] == "JOIN #opechatka"
        assert "PONG :tmi.twitch.tv" in sent
        assert "PRIVMSG #opechatka :Здравствуй, мир" in sent  # one line, no injected command

    asyncio.run(scenario())


def test_twitch_failures_are_fixed_codes_without_the_token():
    async def scenario():
        async with serve(
            twitch_server([], [":tmi.twitch.tv NOTICE * :Login authentication failed\r\n"]),
            "127.0.0.1",
            0,
            logger=QUIET,
        ) as server:
            with pytest.raises(ChatError) as error:
                async with aclosing(twitch(server).events()) as events:
                    async for _ in events:
                        pass
            assert error.value.code == "twitch:denied" and TOKEN not in str(error.value)
        with pytest.raises(ChatError) as error:
            config = TwitchConfig(
                "opechatka", "opechatka", url="ws://127.0.0.1:1", allow_loopback=True
            )
            async with aclosing(TwitchChat(config, SecretStr(TOKEN)).events()) as events:
                async for _ in events:
                    pass
        assert error.value.code == "twitch:connect"

    asyncio.run(scenario())
    with pytest.raises(ValueError):
        TwitchConfig("opechatka", "opechatka", url="ws://203.0.113.5:80", allow_loopback=True)
    with pytest.raises(ValueError):
        TwitchConfig("bad name", "opechatka")


def test_clean_removes_controls_and_bidi_marks():
    assert clean("a\x00b‮c\n\nd  e") == "abc d e"
    assert len(clean("я" * 900)) == 500


class Fake:
    """A fake YouTube: scripted answers per path; remembers every call."""

    def __init__(self, polls):
        self.calls, self.polls, self.expired = [], list(polls), True

    async def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        self.calls.append((method, parts.path.rsplit("/", 1)[-1], headers, body))
        if parts.path.endswith("/token"):
            assert b"refresh_token=refresh-secret" in body
            return 200, {"access_token": "access-1"}
        if headers.get("Authorization") != "Bearer access-1":
            return 401, None
        if parts.path.endswith("/videos"):
            return 200, {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-1"}}]}
        if method == "POST":
            return 200, {}
        query = parse_qs(parts.query)
        assert query["liveChatId"] == ["chat-1"]
        return self.polls.pop(0)


def youtube(fake, **kwargs):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    chat = YouTubeChat(
        YouTubeConfig("abc_DEF-123"), "client", SecretStr("s"), SecretStr("refresh-secret"),
        fetch=fake, sleep=sleep, **kwargs,
    )  # fmt: skip
    return chat, slept


DELETED = {
    "snippet": {"type": "messageDeletedEvent", "messageDeletedDetails": {"deletedMessageId": "y1"}}
}
BANNED = {
    "snippet": {
        "type": "userBannedEvent",
        "userBannedDetails": {"bannedUserDetails": {"channelId": "c9"}},
    }
}


def test_youtube_resolves_the_chat_polls_obeys_the_wait_and_forgets_removals():
    page = (
        200,
        {
            "nextPageToken": "t2",
            "pollingIntervalMillis": 6000,
            "items": [
                {
                    "id": "y1",
                    "snippet": {
                        "type": "textMessageEvent",
                        "publishedAt": "2026-11-04T12:00:00+00:00",
                        "textMessageDetails": {"messageText": "Опечатка, привет"},
                    },
                    "authorDetails": {
                        "channelId": "c1",
                        "displayName": "Ivan",
                        "isChatOwner": True,
                    },
                },
                DELETED,
                BANNED,
                {"snippet": {"type": "superChatEvent"}},
            ],
        },
    )  # fmt: skip

    async def scenario():
        fake = Fake([page, (200, {"items": [], "pollingIntervalMillis": 100})])
        chat, slept = youtube(fake)
        got = []
        async with aclosing(chat.events()) as events:
            async for event in events:
                got.append(event)
                if len(got) == 3:
                    await chat.say("Здравствуйте!")
                    break
        assert isinstance(got[0], ChatMessage) and got[0].privileged and got[0].author == "Ivan"
        assert got[1] == ChatRemoved("youtube", id="y1")
        assert got[2] == ChatRemoved("youtube", author_id="c9")
        post = [c for c in fake.calls if c[0] == "POST" and c[1] == "messages"][0]
        assert (
            json.loads(post[3])["snippet"]["textMessageDetails"]["messageText"] == "Здравствуйте!"
        )
        # The first call had no token (renewed), and the expired one would have been renewed again.
        assert fake.calls[0][1] == "token"

    asyncio.run(scenario())


def test_youtube_errors_are_fixed_codes():
    async def not_live(*args):
        if args[1].endswith("/token"):
            return 200, {"access_token": "t"}
        return 200, {"items": [{"liveStreamingDetails": {}}]}

    async def denied(*args):
        return 400, {"error": "invalid_grant"}

    async def ended(*args):
        if args[1].endswith("/token"):
            return 200, {"access_token": "t"}
        if "videos" in args[1]:
            return 200, {"items": [{"liveStreamingDetails": {"activeLiveChatId": "c"}}]}
        return 403, {"error": {"errors": [{"reason": "liveChatEnded"}]}}

    async def run(fetch):
        chat = YouTubeChat(YouTubeConfig("abc"), "c", SecretStr("s"), SecretStr("r"), fetch=fetch)
        with pytest.raises(ChatError) as error:
            async with aclosing(chat.events()) as events:
                async for _ in events:
                    pass
        return error.value.code

    assert asyncio.run(run(not_live)) == "youtube:not-live"
    assert asyncio.run(run(denied)) == "youtube:denied"
    assert asyncio.run(run(ended)) == "youtube:ended"
    with pytest.raises(ValueError):
        YouTubeConfig("abc", api="https://evil.example/v3")
    with pytest.raises(ValueError):
        YouTubeConfig("../x")
