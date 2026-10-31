"""Channel posts: Telegram formatting (Markdown/HTML -> safe HTML) and pictures with a check."""

import asyncio
import io
import json
import os
import urllib.error

import pytest
from ai_agent_adapters.telegram.bot import TelegramBot, TelegramError
from ai_agent_adapters.telegram.format import telegram_html
from ai_agent_contracts.codec import ContractError
from pydantic import SecretStr

from ai_agent_core.m3_composition.harness_channel import ChannelPost, ChannelSettings

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100


@pytest.mark.parametrize("comments", [False, True])
def test_host_photo_wiring_uses_only_the_selected_personas_workspace(tmp_path, comments):
    from types import SimpleNamespace

    from ai_agent_contracts.common import Scope

    from ai_agent_core.local_host.harness import channel
    from ai_agent_core.local_host.layout import HarnessConfig, HostPaths

    root = tmp_path / "agent-files"
    for persona, content in (("alpha", JPEG), ("beta", JPEG + b"other"), ("", JPEG + b"root")):
        shots = root / persona / "shots"
        shots.mkdir(parents=True)
        (shots / "view.jpg").write_bytes(content)
    providers = SimpleNamespace(
        binding=("https://synthetic.invalid/v1", "synthetic-key"),
        secrets={"AI_PLATFORM_TELEGRAM_BOT_TOKEN": "synthetic-token"},
    )
    settings = HarnessConfig(channel="@synthetic", files_dir=str(root), channel_comments=comments)
    scope = Scope(
        workspace_id="synthetic", persona_id="alpha", memory_space_id="memory", memory_mode="work"
    )
    post = channel(settings, HostPaths(tmp_path), providers, None, model="synthetic", scope=scope)
    assert post.settings.photos == root / "alpha" / "shots"
    assert post._photo("view.jpg") == JPEG
    (root / "alpha" / "shots" / "view.jpg").unlink()
    assert post._photo("view.jpg") is None  # no fallback to another persona or the shared root


def test_markdown_becomes_telegram_html_and_everything_else_is_escaped():
    assert (
        telegram_html("**жирный** и *курсив* и `код`")
        == "<b>жирный</b> и <i>курсив</i> и <code>код</code>"
    )
    assert telegram_html("# Заголовок\n- пункт") == "<b>Заголовок</b>\n• пункт"
    assert telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert telegram_html("~~не~~ [ссылка](https://x.io/a?b=1&c=2)") == (
        '<s>не</s> <a href="https://x.io/a?b=1&amp;c=2">ссылка</a>'
    )
    assert telegram_html("```\n**не трогать** <b>\n```") == "<pre>**не трогать** &lt;b&gt;</pre>"
    assert telegram_html("snake_case_name и 2*3*4") == "snake_case_name и 2*3*4"


def test_the_html_she_writes_keeps_only_the_tags_telegram_knows():
    assert (
        telegram_html("<b>да</b> <strong>да</strong> <em>да</em>")
        == "<b>да</b> <b>да</b> <i>да</i>"
    )
    assert telegram_html("<script>x</script> <img src=1>") == (
        "&lt;script&gt;x&lt;/script&gt; &lt;img src=1&gt;"
    )
    assert telegram_html('<a href="https://x.io">к</a>') == '<a href="https://x.io">к</a>'
    assert telegram_html('<a href="javascript:alert(1)">к</a>') == (
        '&lt;a href="javascript:alert(1)"&gt;к&lt;/a&gt;'
    )


def fake(monkeypatch, answers):
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        answer = answers[min(len(seen), len(answers)) - 1]
        if answer == 400:
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(b'{"ok": true, "result": {"message_id": 5}}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return seen


def test_the_bot_sends_html_and_falls_back_to_plain_text_on_a_400(monkeypatch):
    seen = fake(monkeypatch, [200])
    bot = TelegramBot(SecretStr("1:k"))
    assert bot.send_message("@c", "**привет**") == 5
    assert json.loads(seen[0].data) == {
        "chat_id": "@c",
        "text": "<b>привет</b>",
        "parse_mode": "HTML",
    }

    seen = fake(monkeypatch, [400, 200])  # Telegram could not parse the markup: nothing was posted
    assert bot.send_message("@c", "**привет**") == 5
    assert json.loads(seen[1].data) == {"chat_id": "@c", "text": "**привет**"}

    seen = fake(monkeypatch, [200])
    assert bot.send_photo("@c", JPEG, "*вид*") == 5
    assert b'name="photo"' in seen[0].data and b"<i>" in seen[0].data and b"HTML" in seen[0].data
    with pytest.raises(TelegramError):
        bot.send_photo("@c", b"not a jpeg", "x")


class Bot:
    def __init__(self):
        self.photos = []

    def send_photo(self, chat, jpeg, caption=""):
        self.photos.append((jpeg, caption))
        return 9


def photo_channel(tmp_path, answer):
    shots = tmp_path / "shots"
    shots.mkdir(exist_ok=True)
    (shots / "view.jpg").write_bytes(JPEG)
    seen = []

    async def check(prompt):
        return True, ""

    async def image_check(prompt, image):
        assert "политика" in prompt and image == JPEG  # her list and the picture reach the model
        seen.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer

    settings = ChannelSettings(
        "@t", tmp_path / "rules.txt", tmp_path / "channel.jsonl", photos=shots
    )
    bot = Bot()
    return ChannelPost(settings, bot, check, None, image_check), bot, shots


def lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "channel.jsonl").read_text("utf-8").splitlines()]


def test_a_picture_goes_only_after_the_look_and_stays_out_of_the_journal(tmp_path):
    async def scenario():
        post, bot, _ = photo_channel(tmp_path, (True, ""))
        assert "картинкой" in await post.post_photo("view.jpg", "закат", current=lambda: None)
        assert bot.photos == [(JPEG, "закат")]
        [entry] = lines(tmp_path)
        assert (
            entry["kind"] == "photo"
            and entry["outcome"] == "posted"
            and entry["file"] == "view.jpg"
        )
        assert "base64" not in json.dumps(entry)

        for answer in ((False, "виден чат"), OSError("down")):
            post, bot, _ = photo_channel(tmp_path, answer)
            refused = await post.post_photo("view.jpg", "закат", current=lambda: None)
            assert "Не опубликовано" in refused and bot.photos == []

    asyncio.run(scenario())


def test_only_a_plain_regular_jpeg_in_the_shots_folder_is_read(tmp_path):
    post, _, shots = photo_channel(tmp_path, (True, ""))
    assert post._photo("view.jpg") == JPEG
    for name in ("../view.jpg", "a/b.jpg", "view.png", "missing.jpg", "", ".jpg"):
        assert post._photo(name) is None
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(JPEG)
    os.symlink(outside, shots / "link.jpg")
    assert post._photo("link.jpg") is None  # a link could point at a host file
    (shots / "fake.jpg").write_bytes(b"not an image")
    assert post._photo("fake.jpg") is None
    (shots / "big.jpg").write_bytes(JPEG + b"x" * (8 * 1024 * 1024))
    assert post._photo("big.jpg") is None


def test_a_picture_needs_the_check_and_a_caption(tmp_path):
    post, _, _ = photo_channel(tmp_path, (True, ""))
    with pytest.raises(ContractError):
        asyncio.run(post.post_photo("view.jpg", "  ", current=lambda: None))
    post.image_check = None
    with pytest.raises(ContractError):
        asyncio.run(post.post_photo("view.jpg", "x", current=lambda: None))
