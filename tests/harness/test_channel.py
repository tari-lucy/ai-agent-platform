"""Channel posts: the light check closes the post, STOP cancels the unsent (doc34 V1)."""

import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest
from ai_agent_adapters.telegram.bot import TelegramBot, TelegramError
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode, RunState
from pydantic import SecretStr

from ai_agent_core.m3_composition.harness_channel import ChannelPost, ChannelSettings, verdict
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")


class Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.captions: list[str] = []

    def send_message(self, chat: str, text: str) -> int:
        self.sent.append(("text", text))
        return 7

    def send_voice(self, chat: str, ogg: bytes, caption: str = "") -> int:
        self.sent.append(("voice", ogg))
        self.captions.append(caption)
        return 8


def channel(tmp_path, answer, voice=None):
    async def check(prompt: str) -> tuple[bool, str]:
        assert "политика" in prompt  # the operator's list reaches the check
        if isinstance(answer, Exception):
            raise answer
        return answer

    bot = Bot()
    settings = ChannelSettings("@test", tmp_path / "rules.txt", tmp_path / "channel.jsonl")
    return ChannelPost(settings, bot, check, voice), bot


def journal(tmp_path):
    lines = (tmp_path / "channel.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["outcome"] for line in lines]


def test_allowed_post_goes_refused_or_unavailable_check_does_not(tmp_path):
    async def scenario():
        post, bot = channel(tmp_path, (True, ""))
        assert "Опубликовано" in await post.post("привет", voice=False, current=lambda: None)
        assert bot.sent == [("text", "привет")]
        for answer in ((False, "политика"), OSError("down")):
            post, bot = channel(tmp_path, answer)
            reply = await post.post("пост", voice=False, current=lambda: None)
            assert reply.startswith("Не опубликовано") and bot.sent == []
        assert journal(tmp_path) == ["posted", "refused", "refused"]

    asyncio.run(scenario())


def test_voice_post_and_stop_after_the_check_cancels(tmp_path):
    async def scenario():
        async def voice(text: str) -> bytes:
            return b"OggS..." + text.encode()

        post, bot = channel(tmp_path, (True, ""), voice)
        await post.post("скажи", voice=True, current=lambda: None)
        assert bot.sent == [("voice", "OggS...скажи".encode())]
        assert bot.captions == ["скажи"]

        calls = 0

        def current() -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        post, bot = channel(tmp_path, (True, ""), voice)
        with pytest.raises(ContractError):
            await post.post("после STOP", voice=True, current=current)
        assert bot.sent == []
        text_only, _ = channel(tmp_path, (True, ""))
        with pytest.raises(ContractError):
            await text_only.post("голос", voice=True, current=lambda: None)

    asyncio.run(scenario())


def test_voice_caption_is_checked_with_speech_and_sent_as_one_post(tmp_path):
    async def scenario():
        prompts, spoken = [], []

        async def check(prompt):
            prompts.append(prompt)
            return ("запрещённая подпись" not in prompt, "подпись отклонена")

        async def voice(text):
            spoken.append(text)
            return b"OggS-test"

        post, bot = channel(tmp_path, (True, ""), voice)
        post.check = check
        await post.post("[warm] Привет.", voice=True, caption="Привет.", current=lambda: None)
        assert spoken == ["[warm] Привет."]
        assert bot.sent == [("voice", b"OggS-test")] and bot.captions == ["Привет."]
        assert "[warm] Привет." in prompts[0] and "Подпись под голосовым:\nПривет." in prompts[0]
        entry = json.loads(post.settings.journal.read_text(encoding="utf-8").splitlines()[0])
        assert entry["caption"] == "Привет."
        refused = await post.post(
            "Привет.", voice=True, caption="запрещённая подпись", current=lambda: None
        )
        assert refused.startswith("Не опубликовано") and len(spoken) == len(bot.sent) == 1
        await post.post("Привет.", voice=True, caption="", current=lambda: None)
        assert bot.captions == ["Привет.", ""]

    asyncio.run(scenario())


def test_invalid_caption_is_rejected_before_check_or_synthesis(tmp_path):
    async def scenario():
        async def unused(text):
            pytest.fail("Invalid caption must not reach synthesis or the model check")

        post, bot = channel(tmp_path, (True, ""), unused)
        post.check = unused
        for voice, caption, text in (
            (False, "caption", "text"),
            (True, "x" * 1025, "text"),
            (True, "😀" * 513, "text"),
            (True, None, "😀" * 513),
        ):
            with pytest.raises(ContractError) as error:
                await post.post(text, voice=voice, caption=caption, current=lambda: None)
            assert error.value.error.code == ErrorCode.INVALID_ARGUMENTS
        assert bot.sent == [] and not post.settings.journal.exists()

    asyncio.run(scenario())


def test_stop_during_voice_with_caption_cancels_the_whole_post(tmp_path):
    async def scenario():
        stopped = False

        async def voice(text):
            nonlocal stopped
            stopped = True
            return b"OggS-test"

        def current():
            if stopped:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        post, bot = channel(tmp_path, (True, ""), voice)
        with pytest.raises(ContractError):
            await post.post("text", voice=True, caption="caption", current=current)
        assert bot.sent == [] and bot.captions == []

    asyncio.run(scenario())


def test_verdict_takes_only_a_clear_json_answer():
    assert verdict('{"ok": true}') == (True, "")
    assert verdict('думаю...\n{"ok": false, "reason": "армия"}') == (False, "армия")
    assert verdict("да, можно")[0] is False
    assert verdict('{"ok": "yes"}')[0] is False


def test_unknown_delivery_is_not_reported_as_unpublished_or_retried(tmp_path, monkeypatch):
    post, bot = channel(tmp_path, (True, ""))
    calls = []

    def uncertain(chat, text):
        calls.append(1)
        raise TelegramError("TELEGRAM_OUTCOME_UNKNOWN")

    monkeypatch.setattr(bot, "send_message", uncertain)
    reply = asyncio.run(post.post("synthetic", voice=False, current=lambda: None))
    assert reply.startswith("Исход публикации неизвестен")
    assert "Не повторяй" in reply and calls == [1]
    assert journal(tmp_path) == ["unknown"]


def test_mcp_lists_and_calls_channel_post_only_when_configured(tmp_path):
    async def scenario():
        snapshot = SimpleNamespace(state=RunState.ACTIVE, control_epoch=2, scope=SCOPE)
        rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        bare = await PlatformMcp(rt, lambda: 2).handle(listing)  # type: ignore[arg-type]
        assert "channel_post" not in {t["name"] for t in bare["result"]["tools"]}
        post, bot = channel(tmp_path, (True, ""))
        mcp = PlatformMcp(rt, lambda: 2, None, post)  # type: ignore[arg-type]
        listed = await mcp.handle(listing)
        assert "channel_post" in {t["name"] for t in listed["result"]["tools"]}
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "channel_post", "arguments": {"text": "привет", "voice": False}},
        }
        reply = await mcp.handle(call)
        assert reply["result"]["isError"] is False and bot.sent == [("text", "привет")]

        async def voice(text):
            return b"OggS-test"

        post.voice = voice
        tools = listed["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "channel_post")
        assert "caption" in tool["inputSchema"]["properties"]
        call["params"]["arguments"].update(voice=True, caption="Подпись")
        reply = await mcp.handle(call)
        assert reply["result"]["isError"] is False and bot.captions == ["Подпись"]
        call["params"]["arguments"]["caption"] = None
        reply = await mcp.handle(call)
        assert reply["result"]["isError"] is True and len(bot.sent) == 2
        snapshot.state = RunState.STOPPED
        reply = await mcp.handle(call)
        assert reply["result"]["isError"] is True and len(bot.sent) == 2

    asyncio.run(scenario())


def test_bot_sends_multipart_voice_and_hides_the_token_in_errors(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        if len(seen) > 1:
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(b'{"ok": true, "result": {"message_id": 5}}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    bot = TelegramBot(SecretStr("123:secret"))
    assert bot.send_voice("@test", b"OggSdata", "Подпись") == 5
    assert b"OggSdata" in seen[0].data and b'name="chat_id"' in seen[0].data
    assert b'name="caption"' in seen[0].data and "Подпись".encode() in seen[0].data
    with pytest.raises(TelegramError) as error:
        bot.send_message("@test", "x")
    assert "secret" not in repr(error.value) and error.value.status == 400
