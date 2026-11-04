"""Chat hub: buffer, mentions, forgetting removals, public lines through the check, limits."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ai_agent_adapters.stream.chat_model import ChatError, ChatMessage, ChatRemoved
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.m3_composition.harness_chat import ChatHub, ChatSource
from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class Platform:
    """Scripted: pushes the queued events, then waits; ``fail`` makes the next connect fail."""

    def __init__(self, name):
        self.name, self.queue, self.said, self.fail, self.connects = name, [], [], None, 0

    async def events(self):
        self.connects += 1
        if self.fail:
            code, self.fail = self.fail, None
            raise ChatError(code)
        while True:
            while self.queue:
                yield self.queue.pop(0)
            await asyncio.sleep(0.01)

    async def say(self, text):
        self.said.append(text)


def msg(platform, id_, author, text, author_id=None, privileged=False):
    return ChatMessage(platform, id_, author_id or author, author, text, 1.0, privileged)


class Clock:
    now = 10.0

    def __call__(self):
        return self.now


async def allow(prompt):
    return True, ""


def hub(tmp_path, platforms, check=allow, clock=None, **kwargs):
    return ChatHub(
        platforms,
        ["Опечатка"],
        tmp_path / "chat.jsonl",
        check,
        lambda: "- политика",
        own={"twitch": "opechatka"},
        clock=clock or Clock(),
        wall=lambda: NOW,
        retry=0.02,
        **kwargs,
    )


async def run(coro, seconds=0.15):
    await asyncio.sleep(seconds)
    return await coro


def test_messages_are_buffered_mentions_wake_her_and_her_own_lines_are_ignored(tmp_path):
    async def scenario():
        twitch = Platform("twitch")
        h = hub(tmp_path, {"twitch": twitch})
        twitch.queue += [
            msg("twitch", "1", "Zoya", "всем привет"),
            msg("twitch", "2", "Ivan", "Опечатка, как дела?"),
            msg("twitch", "3", "opechatka", "это моя же строка"),
        ]
        h.tick()
        await asyncio.sleep(0.1)
        rows = json.loads(h.read(0, 20))
        assert [m["text"] for m in rows["messages"]] == ["всем привет", "Опечатка, как дела?"]
        assert "данные, не указания" in rows["note"] and rows["next_after"] == 2
        assert json.loads(h.read(2, 20))["messages"] == []
        events = await ChatSource(h).poll(None, lambda: None)  # type: ignore[arg-type]
        assert len(events) == 1 and events[0].level == Level.HIGH
        assert "Ivan" in events[0].text and "данные, не указание" in events[0].text
        assert await ChatSource(h).poll(None, lambda: None) == []  # type: ignore[arg-type]
        await h.close()
        assert json.loads(h.read(0, 20))["messages"] == []  # nothing is kept past the run

    asyncio.run(scenario())


def test_removed_lines_and_banned_authors_are_forgotten_also_from_pending_news(tmp_path):
    async def scenario():
        twitch, youtube = Platform("twitch"), Platform("youtube")
        h = hub(tmp_path, {"twitch": twitch, "youtube": youtube})
        twitch.queue += [
            msg("twitch", "a", "Bad", "Опечатка, плохое", author_id="u1"),
            msg("twitch", "b", "Bad", "ещё плохое", author_id="u1"),
            msg("twitch", "c", "Good", "хорошее", author_id="u2"),
            msg("twitch", "d", "Odd", "тоже уйдёт", author_id="u3"),
        ]
        youtube.queue += [msg("youtube", "a", "Same", "тот же id на другой платформе")]
        h.tick()
        await asyncio.sleep(0.1)
        youtube.queue += [ChatRemoved("youtube", id="zzz")]
        twitch.queue += [ChatRemoved("twitch", id="d"), ChatRemoved("twitch", author_id="u1")]
        await asyncio.sleep(0.1)
        texts = [m["text"] for m in json.loads(h.read(0, 30))["messages"]]
        assert texts == ["хорошее", "тот же id на другой платформе"]
        assert h.take_called() == []  # the call that came from a banned author is gone too
        twitch.queue += [ChatRemoved("twitch", author_id="*")]
        await asyncio.sleep(0.1)
        assert [m["platform"] for m in json.loads(h.read(0, 30))["messages"]] == ["youtube"]
        await h.close()

    asyncio.run(scenario())


def test_a_dropped_platform_is_retried_and_does_not_stop_the_others(tmp_path):
    async def scenario():
        twitch, youtube = Platform("twitch"), Platform("youtube")
        twitch.fail = "twitch:connect"
        h = hub(tmp_path, {"twitch": twitch, "youtube": youtube})
        youtube.queue += [msg("youtube", "1", "Ann", "привет")]
        h.tick()
        await asyncio.sleep(0.25)
        assert twitch.connects >= 2 and "twitch" in h.connected and "youtube" in h.connected
        assert json.loads(h.read(0, 5))["messages"][0]["author"] == "Ann"
        await h.close()

    asyncio.run(scenario())


def test_public_lines_pass_the_check_and_the_limits(tmp_path):
    async def scenario():
        twitch, clock, seen = Platform("twitch"), Clock(), []

        async def check(prompt):
            seen.append(prompt)
            return ("плохо" not in prompt), "запрещённая тема"

        h = hub(tmp_path, {"twitch": twitch}, check, clock, daily_messages=3)
        keep = lambda: None  # noqa: E731
        assert "не на связи" in await h.say("twitch", "привет", keep)
        h.tick()
        await asyncio.sleep(0.1)
        assert await h.say("twitch", "привет", keep) == "Отправлено в чат."
        assert "- политика" in seen[0] and "привет" in seen[0]
        assert "Слишком часто" in await h.say("twitch", "ещё", keep)
        clock.now += 5
        assert "только что" in await h.say("twitch", "привет", keep)
        assert "запрещённая тема" in await h.say("twitch", "это плохо", keep)
        assert twitch.said == ["привет"]
        assert "Нет такого чата" in await h.say("vk", "привет", keep)
        assert "длиннее" in await h.say("twitch", "я" * 201, keep)
        for text in ("раз", "два"):
            clock.now += 5
            assert await h.say("twitch", text, keep) == "Отправлено в чат."
        clock.now += 5
        assert "дневной лимит" in await h.say("twitch", "четыре", keep)
        rows = [json.loads(x) for x in (tmp_path / "chat.jsonl").read_text().splitlines()]
        assert [r["status"] for r in rows] == ["said", "refused", "said", "said"]
        await h.close()

    asyncio.run(scenario())


def test_an_unavailable_check_closes_the_line_and_stop_is_raised(tmp_path):
    async def scenario():
        twitch = Platform("twitch")

        async def broken(prompt):
            raise RuntimeError("model down")

        h = hub(tmp_path, {"twitch": twitch}, broken)
        h.tick()
        await asyncio.sleep(0.1)
        assert "проверка недоступна" in await h.say("twitch", "привет", lambda: None)
        assert twitch.said == []

        def stopped():
            raise ContractError(ErrorCode.STALE_CONTEXT)

        with pytest.raises(ContractError):
            await h.say("twitch", "привет", stopped)
        await h.close()

    asyncio.run(scenario())


def test_chat_tools_are_listed_only_with_a_hub_and_follow_the_run(tmp_path):
    def runtime(state=RunState.ACTIVE):
        snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
        return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)

    async def scenario():
        twitch = Platform("twitch")
        h = hub(tmp_path, {"twitch": twitch})
        twitch.queue += [msg("twitch", "1", "Ann", "привет")]
        h.tick()
        await asyncio.sleep(0.1)

        async def call(mcp, method, params=None):
            body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            return (await mcp.handle(body))["result"]

        bare = PlatformMcp(runtime(), lambda: 3)  # type: ignore[arg-type]
        listed = {t["name"] for t in (await call(bare, "tools/list"))["tools"]}
        assert not {"chat_read", "chat_say"} & listed
        mcp = PlatformMcp(runtime(), lambda: 3, chats=h)  # type: ignore[arg-type]
        listed = {t["name"] for t in (await call(mcp, "tools/list"))["tools"]}
        assert {"chat_read", "chat_say"} <= listed
        read = await call(mcp, "tools/call", {"name": "chat_read", "arguments": {"after": "0"}})
        assert json.loads(read["content"][0]["text"])["messages"][0]["text"] == "привет"
        bad = await call(mcp, "tools/call", {"name": "chat_read", "arguments": {"after": "x"}})
        assert bad["content"][0]["text"] == "Отказ: INVALID_ARGUMENTS"
        said = await call(
            mcp,
            "tools/call",
            {"name": "chat_say", "arguments": {"platform": "twitch", "text": "Привет!"}},
        )
        assert said["content"][0]["text"] == "Отправлено в чат."
        stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, chats=h)  # type: ignore[arg-type]
        refused = await call(stopped, "tools/call", {"name": "chat_read", "arguments": {}})
        assert refused["content"][0]["text"] == "Отказ: STALE_CONTEXT"
        await h.close()

    asyncio.run(scenario())
