"""Discussion routing, durable cursors and no duplicate public replies; no live APIs."""

import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest
from ai_agent_adapters.telegram.bot import TelegramBot, TelegramError
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.m3_composition.harness_channel import ChannelPost, ChannelSettings
from ai_agent_core.m3_composition.harness_comments import TelegramComments
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")


def root():
    return {
        "message_id": 10,
        "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "chat": {"id": -1001}, "message_id": 7},
    }


def update(uid=101, mid=11, text="Вопрос читателя", **overrides):
    return {
        "update_id": uid,
        "message": {
            "message_id": mid,
            "chat": {"id": -1002},
            "from": {"id": 42, "first_name": "Читатель"},
            "date": int(time.time()),
            "reply_to_message": root(),
            "text": text,
            **overrides,
        },
    }


class Bot(TelegramBot):
    def __init__(self):
        self.rows = [update()]
        self.offsets = []
        self.sent = []
        self.webhook = ""
        self.status = "administrator"
        self.failure = None
        self.on_poll = lambda: None

    def metadata(self, method, **arguments):
        if method == "getWebhookInfo":
            return {"url": self.webhook}
        if method == "getMe":
            return {"id": 99, "can_read_all_group_messages": False}
        if method == "getChatMember":
            return {"status": self.status}
        if arguments["chat_id"] == "@test":
            return {"id": -1001, "type": "channel", "linked_chat_id": -1002}
        return {"id": -1002, "type": "supergroup", "linked_chat_id": -1001}

    def updates(self, offset):
        self.offsets.append(offset)
        self.on_poll()
        return [r for r in self.rows if offset is None or r["update_id"] >= offset]

    def reply_message(self, chat, message_id, text):
        self.sent.append((chat, message_id, text))
        if self.failure:
            raise self.failure
        return 501


def service(tmp_path, bot=None):
    bot = bot or Bot()

    async def check(prompt):
        return "ЗАПРЕТ" not in prompt, "отказ"

    channel = ChannelPost(
        ChannelSettings("@test", tmp_path / "rules.txt", tmp_path / "channel.jsonl"), bot, check
    )
    inbox = TelegramComments(channel, bot, SCOPE, tmp_path)
    channel.comments = inbox
    return channel, inbox, bot


def test_collects_only_comments_on_own_posts_and_persists_cursor(tmp_path):
    async def scenario():
        channel, inbox, bot = service(tmp_path)
        bot.rows += [
            update(102, 12, "Чужая группа", chat={"id": -999}),
            update(103, 13, "Просто сообщение", reply_to_message={}),
            update(104, 14, "Бот", **{"from": {"id": 99, "is_bot": True}}),
            update(
                105,
                15,
                "Другой канал",
                reply_to_message={
                    **root(),
                    "forward_origin": {
                        "type": "channel",
                        "chat": {"id": -999},
                        "message_id": 7,
                    },
                },
            ),
            update(106, 16, "Ответ читателю", reply_to_message={"message_id": 11}),
        ]
        first = await inbox.read(SCOPE, lambda: None, limit=1)
        assert len(first["items"]) == 1 and first["items"][0]["post_id"] == 7
        assert first["items"][0]["author"]["telegram_user_id"] == 42
        restarted = TelegramComments(channel, bot, SCOPE, tmp_path)
        second = await restarted.read(SCOPE, lambda: None, after=first["next_after"])
        assert [x["text"] for x in second["items"]] == ["Ответ читателю"]
        assert bot.offsets == [None, 107]
        stored = inbox.path.read_text(encoding="utf-8")
        assert len(json.loads(stored)["comments"]) == 2

    asyncio.run(scenario())


def test_moderated_reply_uses_discussion_and_exact_target_replay_is_not_sent(tmp_path):
    async def scenario():
        channel, inbox, bot = service(tmp_path)
        rows = await inbox.read(SCOPE, lambda: None)
        cid = rows["items"][0]["comment_id"]
        assert (await inbox.reply(SCOPE, lambda: None, cid, "ЗАПРЕТ"))["error"]
        assert bot.sent == []
        first = await inbox.reply(SCOPE, lambda: None, cid, "Спасибо за вопрос")
        restarted = TelegramComments(channel, bot, SCOPE, tmp_path)
        assert await restarted.reply(SCOPE, lambda: None, cid, "Спасибо за вопрос") == first
        assert bot.sent == [("-1002", 11, "Спасибо за вопрос")]
        bot.rows.append(
            {
                "update_id": 107,
                "edited_message": {
                    **update(107)["message"],
                    "text": "Изменённый вопрос",
                },
            }
        )
        result = await inbox.reply(SCOPE, lambda: None, cid, "Ещё один ответ")
        assert result["error"] == "COMMENT_UNAVAILABLE_OR_CHANGED" and len(bot.sent) == 1

    asyncio.run(scenario())


def test_unknown_send_blocks_even_changed_retry_and_survives_restart(tmp_path):
    async def scenario():
        channel, inbox, bot = service(tmp_path)
        cid = (await inbox.read(SCOPE, lambda: None))["items"][0]["comment_id"]
        bot.failure = TelegramError("TELEGRAM_OUTCOME_UNKNOWN")
        first = await inbox.reply(SCOPE, lambda: None, cid, "Ответ")
        assert first["delivery"] == "unknown"
        restarted = TelegramComments(channel, bot, SCOPE, tmp_path)
        assert (await restarted.reply(SCOPE, lambda: None, cid, "Ответ"))["delivery"] == "unknown"
        changed = await restarted.reply(SCOPE, lambda: None, cid, "Ответ другими словами")
        assert changed["error"] == "TELEGRAM_OUTCOME_UNKNOWN" and len(bot.sent) == 1

    asyncio.run(scenario())


def test_webhook_privacy_scope_and_expired_sources_fail_closed(tmp_path):
    async def scenario():
        _, inbox, bot = service(tmp_path)
        bot.webhook = "https://synthetic.invalid/hook"
        assert (await inbox.read(SCOPE, lambda: None))["error"] == "TELEGRAM_WEBHOOK_IN_USE"
        assert bot.offsets == []
        bot.webhook, bot.status = "", "member"
        private = await inbox.read(SCOPE, lambda: None)
        assert private["error"] == "TELEGRAM_GROUP_READ_ACCESS_REQUIRED"
        other = SCOPE.model_copy(update={"persona_id": "other"})
        with pytest.raises(ContractError) as error:
            await inbox.read(other, lambda: None)
        assert error.value.error.code == ErrorCode.ACCESS_DENIED
        bot.status = "administrator"
        bot.rows[0]["message"]["date"] = int(time.time()) - 8 * 86400
        assert (await inbox.read(SCOPE, lambda: None))["items"] == []

    asyncio.run(scenario())


def test_stop_during_receive_or_check_releases_no_text_or_reply(tmp_path):
    async def scenario():
        channel, inbox, bot = service(tmp_path)
        stopped = False

        def current():
            if stopped:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        def stop():
            nonlocal stopped
            stopped = True

        bot.on_poll = stop
        with pytest.raises(ContractError):
            await inbox.read(SCOPE, current)
        assert not inbox.path.exists()
        bot.on_poll, stopped = lambda: None, False
        cid = (await inbox.read(SCOPE, current))["items"][0]["comment_id"]

        async def check(prompt):
            stop()
            return True, ""

        channel.check = check
        with pytest.raises(ContractError):
            await inbox.reply(SCOPE, current, cid, "Ответ")
        assert bot.sent == [] and not json.loads(inbox.path.read_text())["replies"]

    asyncio.run(scenario())


def test_mcp_lists_and_routes_comment_tools_only_with_enabled_service(tmp_path):
    async def scenario():
        channel, _, bot = service(tmp_path)
        snapshot = SimpleNamespace(state=RunState.ACTIVE, control_epoch=2, scope=SCOPE)
        rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)
        mcp = PlatformMcp(rt, lambda: 2, channel=channel)
        listed = await mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert {"channel_comments", "channel_reply"} <= {
            t["name"] for t in listed["result"]["tools"]
        }
        reply = await mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "channel_comments", "arguments": {}},
            }
        )
        data = json.loads(reply["result"]["content"][0]["text"])
        assert data["items"][0]["post_id"] == 7
        snapshot.state = RunState.STOPPED
        stopped = await mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "channel_comments", "arguments": {}},
            }
        )
        assert stopped["result"]["isError"] is True and bot.offsets == [None]

    asyncio.run(scenario())


def test_adapter_preserves_target_and_never_changes_update_subscriptions(monkeypatch):
    from pydantic import SecretStr

    requests = []

    def urlopen(request, timeout):
        arguments = json.loads(request.data)
        requests.append(arguments)
        result = [] if request.full_url.endswith("getUpdates") else {"message_id": 21}
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    bot = TelegramBot(SecretStr("synthetic-token"))
    assert bot.updates(123) == []
    assert requests[0] == {"limit": 20, "timeout": 0, "offset": 123}
    assert bot.reply_message("-1002", 11, "Ответ") == 21
    assert requests[1]["chat_id"] == "-1002"
    assert requests[1]["reply_parameters"] == {
        "message_id": 11,
        "allow_sending_without_reply": False,
    }


def test_group_right_withdrawn_during_moderation_prevents_reply(tmp_path):
    async def scenario():
        channel, inbox, bot = service(tmp_path)
        cid = (await inbox.read(SCOPE, lambda: None))["items"][0]["comment_id"]

        async def check(prompt):
            bot.status = "member"
            return True, ""

        channel.check = check
        result = await inbox.reply(SCOPE, lambda: None, cid, "Ответ")
        assert result["error"] == "TELEGRAM_GROUP_READ_ACCESS_REQUIRED" and bot.sent == []

    asyncio.run(scenario())


def test_control_guard_stays_on_owner_thread_while_network_runs_off_loop(tmp_path):
    async def scenario():
        _, inbox, bot = service(tmp_path)
        owner = threading.get_ident()
        checks, network_threads = [], []
        original_updates, original_reply = bot.updates, bot.reply_message

        def current():
            checks.append(threading.get_ident())
            if checks[-1] != owner:
                raise RuntimeError("CONTROL_OWNER_THREAD_REQUIRED")

        def updates(offset):
            network_threads.append(threading.get_ident())
            return original_updates(offset)

        def reply(chat, message_id, text):
            network_threads.append(threading.get_ident())
            return original_reply(chat, message_id, text)

        bot.updates, bot.reply_message = updates, reply
        page = await inbox.read(SCOPE, current)
        result = await inbox.reply(SCOPE, current, page["items"][0]["comment_id"], "Ответ")
        assert result["delivery"] == "sent"
        assert set(checks) == {owner} and all(t != owner for t in network_threads)

    asyncio.run(scenario())


def test_host_wiring_is_opt_in_and_partitioned(tmp_path):
    from ai_agent_core.local_host.harness import channel
    from ai_agent_core.local_host.layout import HarnessConfig, HostPaths

    providers = SimpleNamespace(
        binding=("https://synthetic.invalid/v1", "synthetic-key"),
        secrets={"AI_PLATFORM_TELEGRAM_BOT_TOKEN": "synthetic-token"},
    )
    settings = HarnessConfig(channel="@test")
    post = channel(settings, HostPaths(tmp_path), providers, None, model="synthetic", scope=SCOPE)
    assert post.comments is None
    enabled = settings.model_copy(update={"channel_comments": True})
    post = channel(enabled, HostPaths(tmp_path), providers, None, model="synthetic", scope=SCOPE)
    assert post.comments.partition == SCOPE.partition
    assert not post.comments.path.exists()
