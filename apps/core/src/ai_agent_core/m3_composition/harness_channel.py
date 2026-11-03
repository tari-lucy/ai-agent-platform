"""Posts to the persona's Telegram channel (doc34 V1, operator 2026-10-29).

Text or a voice message, published at once. Before sending, a short check by the same model
against the operator's list of forbidden topics (a light moderator, D75): a refusal, a broken
answer or an unavailable check closes the post, and the agent gets the reason. STOP between
any two steps cancels what is not sent yet; a sent post is not taken back. The journal is
local and may hold personal text: never in Git or reports.
"""

import asyncio
import base64
import json
import logging
import os
import re
import stat
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

if TYPE_CHECKING:
    from .harness_comments import TelegramComments
    from .harness_host import Usage

LOG = logging.getLogger(__name__)
MAX_TEXT, MAX_VOICE_TEXT, MAX_CAPTION = 4000, 1000, 1024
MAX_PHOTO = 8 * 1024 * 1024
PHOTO_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. -]{0,100}\.jpe?g", re.IGNORECASE)

DEFAULT_RULES = """\
# Чего личность не касается публично: канал, чат игры, стрим. По строке на тему.
призывы к насилию, экстремизму, терроризму, массовым беспорядкам; их оправдание
армия и СВО: оценки, «фейки», дискредитация, сведения о потерях и передвижениях
наркотики и другие запрещённые вещества: как достать, употреблять, их одобрение
запрещённые в РФ организации, а также иноагенты без обязательной пометки
самоубийство и самоповреждение: способы, романтизация, призывы
оскорбление власти, государственных символов, чувств верующих
«пропаганда» по закону о ЛГБТ, порнография, сексуальное содержание с несовершеннолетними
политика: выборы, партии, протесты, санкции, конфликты государств
личные данные людей без их согласия, клевета, травля
призывы обходить блокировки, советы по нарушению закона
"""

CHECK = (
    "Ты — проверяющий перед публикацией в Telegram-канале, который читают в России. "
    "Запрещённые темы:\n{rules}\n"
    "Пост:\n<<<\n{text}\n>>>\n"
    "Касается ли пост хотя бы одной запрещённой темы, прямо или намёком? Шутки и цитаты "
    "тоже считаются. Ответь только JSON без пояснений вокруг: "
    '{{"ok": true}} если можно публиковать, иначе {{"ok": false, "reason": "коротко почему"}}.'
)

CHECK_IMAGE = (
    "Ты — проверяющий перед публикацией в Telegram-канале, который читают в России. "
    "Запрещённые темы:\n{rules}\n"
    "К посту приложена картинка из игры. Подпись:\n<<<\n{text}\n>>>\n"
    "Посмотри на картинку и подпись. Закрой пост, если запрещённой темы касается подпись или "
    "картинка, а также если на картинке видны чужие ники или реплики из чата, адреса, "
    "координаты, личные данные или иной посторонний текст, который не нужен для кадра. "
    "Ответь только JSON без пояснений вокруг: "
    '{{"ok": true}} если можно публиковать, иначе {{"ok": false, "reason": "коротко почему"}}.'
)


class Bot(Protocol):
    def send_message(self, chat: str, text: str) -> int: ...
    def send_voice(self, chat: str, ogg: bytes, caption: str = "") -> int: ...
    def send_photo(self, chat: str, jpeg: bytes, caption: str = "") -> int: ...


Voice = Callable[[str], Awaitable[bytes]]  # text -> Ogg/Opus
Check = Callable[[str], Awaitable[tuple[bool, str]]]  # prompt -> (ok, reason)
ImageCheck = Callable[[str, bytes], Awaitable[tuple[bool, str]]]  # prompt, JPEG -> (ok, reason)
Describe = Callable[[str, bytes], Awaitable[str]]  # prompt, JPEG -> the model's answer


@dataclass(frozen=True)
class ChannelSettings:
    chat: str
    rules_file: Path
    journal: Path
    daily_posts: int = 3
    photos: Path | None = None  # the folder her pictures are read from (no links, plain names)


class ChannelPost:
    def __init__(
        self,
        settings: ChannelSettings,
        bot: Bot,
        check: Check,
        voice: Voice | None = None,
        image_check: ImageCheck | None = None,
    ) -> None:
        self.settings, self.bot, self.check, self.voice = settings, bot, check, voice
        self.image_check = image_check
        self.lock = asyncio.Lock()
        self.comments: TelegramComments | None = None

    def rules(self) -> str:
        return public_rules(self.settings.rules_file)

    async def post(
        self,
        text: str,
        *,
        voice: bool,
        current: Callable[[], None],
        caption: str | None = None,
    ) -> str:
        """Publish or refuse; ``current`` raises once STOP or a new epoch happened."""
        text = text.strip()
        if not text or len(text) > (MAX_VOICE_TEXT if voice else MAX_TEXT):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if caption is not None and (not voice or not isinstance(caption, str)):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        caption = (text if caption is None else caption.strip()) if voice else None
        if caption is not None and len(caption.encode("utf-16-le")) // 2 > MAX_CAPTION:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if voice and self.voice is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        async with self.lock:  # one post at a time: the journal and the channel stay in order
            current()
            if self.posted_today() >= self.settings.daily_posts:
                return (
                    f"Не опубликовано: сегодня уже {self.settings.daily_posts} постов, "
                    "это дневной лимит. Продолжи завтра или запиши мысль в дневник."
                )
            try:
                checked = text
                if caption and caption != text:
                    checked = f"Текст озвучки:\n{text}\n\nПодпись под голосовым:\n{caption}"
                ok, reason = await self.check(CHECK.format(rules=self.rules(), text=checked))
            except Exception:  # noqa: BLE001 - an unavailable check closes the post
                LOG.warning("Channel post check unavailable")
                ok, reason = False, "проверка недоступна, попробуй позже"
            current()
            if not ok:
                self._journal(text, voice, "refused", reason, caption)
                return f"Не опубликовано: {reason or 'запрещённая тема'}. Перепиши или откажись."
            audio = None
            if voice:
                assert self.voice is not None
                try:
                    audio = await self.voice(text)
                except Exception as error:  # noqa: BLE001 - fixed code only, no remote text
                    code = getattr(error, "code", type(error).__name__)
                    self._journal(text, voice, "failed", str(code), caption)
                    return f"Не опубликовано: синтез голоса не удался ({code})."
                current()
            try:
                if audio is None:
                    message = await asyncio.to_thread(
                        self.bot.send_message, self.settings.chat, text
                    )
                else:
                    message = await asyncio.to_thread(
                        self.bot.send_voice, self.settings.chat, audio, caption or ""
                    )
            except Exception as error:  # noqa: BLE001 - fixed code only
                code = str(getattr(error, "code", type(error).__name__))
                status = getattr(error, "status", None)
                if code == "TELEGRAM_OUTCOME_UNKNOWN":
                    self._journal(text, voice, "unknown", code, caption)
                    return (
                        "Исход публикации неизвестен: подтверждение Telegram не получено. "
                        "Не повторяй пост до проверки канала."
                    )
                self._journal(
                    text, voice, "failed", code if status is None else f"{code}:{status}", caption
                )
                return f"Не опубликовано: Telegram не принял ({code})."
            self._journal(text, voice, "posted", str(message), caption)
            return "Опубликовано в канал" + (" голосовым." if voice else ".")

    async def post_photo(self, name: str, text: str, *, current: Callable[[], None]) -> str:
        """A picture from her shots folder with a caption; a model looks at both before it goes."""
        text = text.strip()
        if not text or len(text.encode("utf-16-le")) // 2 > MAX_CAPTION:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if self.image_check is None or self.settings.photos is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        async with self.lock:
            current()
            if self.posted_today() >= self.settings.daily_posts:
                return (
                    f"Не опубликовано: сегодня уже {self.settings.daily_posts} постов, "
                    "это дневной лимит. Продолжи завтра или запиши мысль в дневник."
                )
            image = await asyncio.to_thread(self._photo, name)
            if image is None:
                return (
                    "Не опубликовано: нет такого файла. Нужен JPEG в /workspace/shots, "
                    "имя без пути, до 8 МБ."
                )
            try:
                prompt = CHECK_IMAGE.format(rules=self.rules(), text=text)
                ok, reason = await self.image_check(prompt, image)
            except Exception:  # noqa: BLE001 - an unavailable check closes the post
                LOG.warning("Channel photo check unavailable")
                ok, reason = False, "проверка недоступна, попробуй позже"
            current()
            if not ok:
                self._journal(text, False, "refused", reason, kind="photo", file=name)
                return f"Не опубликовано: {reason or 'запрещённая тема'}. Сделай другой кадр."
            try:
                message = await asyncio.to_thread(
                    self.bot.send_photo, self.settings.chat, image, text
                )
            except Exception as error:  # noqa: BLE001 - fixed code only
                code = str(getattr(error, "code", type(error).__name__))
                status = getattr(error, "status", None)
                if code == "TELEGRAM_OUTCOME_UNKNOWN":
                    self._journal(text, False, "unknown", code, kind="photo", file=name)
                    return (
                        "Исход публикации неизвестен: подтверждение Telegram не получено. "
                        "Не повторяй пост до проверки канала."
                    )
                detail = code if status is None else f"{code}:{status}"
                self._journal(text, False, "failed", detail, kind="photo", file=name)
                return f"Не опубликовано: Telegram не принял ({code})."
            self._journal(text, False, "posted", str(message), kind="photo", file=name)
            return "Опубликовано в канал картинкой."

    def _photo(self, name: str) -> bytes | None:
        return read_photo(self.settings.photos, name)

    def posted_today(self) -> int:
        """Posts that reached the channel today (host-local day), from the local journal."""
        today = datetime.now().astimezone().date()
        try:
            lines = self.settings.journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        count = 0
        for line in lines:
            try:
                entry = json.loads(line)
                moment = datetime.fromisoformat(entry["at"]).astimezone()
            except ValueError, KeyError, TypeError:
                continue
            count += entry.get("outcome") == "posted" and moment.date() == today
        return count

    def _journal(
        self,
        text: str,
        voice: bool,
        outcome: str,
        detail: str,
        caption: str | None = None,
        *,
        kind: str | None = None,
        file: str | None = None,
    ) -> None:
        path = self.settings.journal
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, str | None] = {
            "at": datetime.now(UTC).isoformat(),
            "kind": kind or ("voice" if voice else "text"),
            "outcome": outcome,
            "detail": detail[:300],
            "text": text,
        }
        if voice:
            entry["caption"] = caption
        if file is not None:
            entry["file"] = file
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_photo(folder: Path | None, name: str) -> bytes | None:
    """The file's bytes, or None: plain name, no links, a regular JPEG within the limit."""
    if folder is None or not PHOTO_NAME.fullmatch(name) or folder.is_symlink():
        return None
    try:
        descriptor = os.open(folder / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PHOTO:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_PHOTO + 1)
    finally:
        os.close(descriptor)
    return data if len(data) <= MAX_PHOTO and data.startswith(b"\xff\xd8\xff") else None


def public_rules(path: Path) -> str:
    """The operator's list of topics never spoken of in public, as prompt lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_RULES, encoding="utf-8")
        text = DEFAULT_RULES
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(f"- {line}" for line in lines if line and not line.startswith("#"))


class Spent(Exception):
    """The harness's daily money is gone: no model call is made."""


def model_check(upstream: str, key: str, model: str, usage: Usage | None) -> Check:
    """The check as one call to the persona's model, charged to the harness's daily budget."""
    asked = _completion(upstream, key, model, usage, 300)

    async def check(prompt: str) -> tuple[bool, str]:
        try:
            return verdict(await asked(prompt, None))
        except Spent:
            return False, "дневной бюджет исчерпан"

    return check


def model_image_check(upstream: str, key: str, model: str, usage: Usage | None) -> ImageCheck:
    """The same call with the picture attached as a data URL (a vision-capable model)."""
    asked = _completion(upstream, key, model, usage, 300)

    async def check(prompt: str, image: bytes) -> tuple[bool, str]:
        try:
            return verdict(await asked(prompt, image))
        except Spent:
            return False, "дневной бюджет исчерпан"

    return check


def model_describe(
    upstream: str, key: str, model: str, usage: Usage | None, max_tokens: int = 400
) -> Describe:
    """One look at a picture: the model's plain answer, charged to the daily budget (or Spent)."""
    asked = _completion(upstream, key, model, usage, max_tokens)

    async def describe(prompt: str, image: bytes) -> str:
        return await asked(prompt, image)

    return describe


def _completion(
    upstream: str, key: str, model: str, usage: Usage | None, max_tokens: int
) -> Callable[[str, bytes | None], Awaitable[str]]:
    def call(prompt: str, image: bytes | None) -> dict[str, Any]:
        content: Any = prompt
        if image is not None:
            url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]
        request = urllib.request.Request(
            upstream.rstrip("/") + "/chat/completions",
            data=json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                }
            ).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "ai-agent-platform/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            reply: Any = json.loads(response.read(4 * 1024 * 1024))
        return reply if isinstance(reply, dict) else {}

    async def ask(prompt: str, image: bytes | None) -> str:
        if usage is not None:
            remaining = usage.remaining()
            if remaining is not None and remaining <= 0:
                raise Spent
        reply = await asyncio.to_thread(call, prompt, image)
        if usage is not None:
            raw = reply.get("usage")
            tokens: dict[str, Any] = raw if isinstance(raw, dict) else {}
            details = tokens.get("prompt_tokens_details") or {}
            usage.record(
                {
                    "kind": "usage",
                    "status": 200,
                    "model": model,
                    "input": tokens.get("prompt_tokens"),
                    "output": tokens.get("completion_tokens"),
                    "cached": details.get("cached_tokens") if isinstance(details, dict) else None,
                }
            )
        choices = reply.get("choices") or [{}]
        text = str((choices[0].get("message") or {}).get("content") or "")
        if image is not None:  # what came back, never what was said
            LOG.info(
                "Picture call: model=%s answered=%s usage=%s finish=%s chars=%d",
                model,
                reply.get("model"),
                isinstance(reply.get("usage"), dict),
                choices[0].get("finish_reason"),
                len(text),
            )
        return text

    return ask


def verdict(content: str) -> tuple[bool, str]:
    """The first JSON object in the answer; anything else is a refusal."""
    found = re.search(r"\{.*?\}", content, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return False, "проверка ответила непонятно"
    return data["ok"], str(data.get("reason") or "")[:300]
