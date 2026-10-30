"""Posts to the persona's Telegram channel (doc34 V1, operator 2026-10-29).

Text or a voice message, published at once. Before sending, a short check by the same model
against the operator's list of forbidden topics (a light moderator, D75): a refusal, a broken
answer or an unavailable check closes the post, and the agent gets the reason. STOP between
any two steps cancels what is not sent yet; a sent post is not taken back. The journal is
local and may hold personal text: never in Git or reports.
"""

import asyncio
import json
import logging
import re
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

if TYPE_CHECKING:
    from .harness_host import Usage

LOG = logging.getLogger(__name__)
MAX_TEXT, MAX_VOICE_TEXT, MAX_CAPTION = 4000, 1000, 1024

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


class Bot(Protocol):
    def send_message(self, chat: str, text: str) -> int: ...
    def send_voice(self, chat: str, ogg: bytes, caption: str = "") -> int: ...


Voice = Callable[[str], Awaitable[bytes]]  # text -> Ogg/Opus
Check = Callable[[str], Awaitable[tuple[bool, str]]]  # prompt -> (ok, reason)


@dataclass(frozen=True)
class ChannelSettings:
    chat: str
    rules_file: Path
    journal: Path


class ChannelPost:
    def __init__(
        self, settings: ChannelSettings, bot: Bot, check: Check, voice: Voice | None = None
    ) -> None:
        self.settings, self.bot, self.check, self.voice = settings, bot, check, voice
        self.lock = asyncio.Lock()

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

    def _journal(
        self, text: str, voice: bool, outcome: str, detail: str, caption: str | None = None
    ) -> None:
        path = self.settings.journal
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, str | None] = {
            "at": datetime.now(UTC).isoformat(),
            "kind": "voice" if voice else "text",
            "outcome": outcome,
            "detail": detail[:300],
            "text": text,
        }
        if voice:
            entry["caption"] = caption
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


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


def model_check(upstream: str, key: str, model: str, usage: Usage | None) -> Check:
    """The check as one call to the persona's model, charged to the harness's daily budget."""

    def call(prompt: str) -> dict[str, Any]:
        request = urllib.request.Request(
            upstream.rstrip("/") + "/chat/completions",
            data=json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 300,
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

    async def check(prompt: str) -> tuple[bool, str]:
        if usage is not None:
            remaining = usage.remaining()
            if remaining is not None and remaining <= 0:
                return False, "дневной бюджет исчерпан"
        reply = await asyncio.to_thread(call, prompt)
        if usage is not None:
            raw = reply.get("usage")
            tokens: dict[str, Any] = raw if isinstance(raw, dict) else {}
            details = tokens.get("prompt_tokens_details") or {}
            usage.record(
                {
                    "kind": "usage",
                    "status": 200,
                    "input": tokens.get("prompt_tokens"),
                    "output": tokens.get("completion_tokens"),
                    "cached": details.get("cached_tokens") if isinstance(details, dict) else None,
                }
            )
        choices = reply.get("choices") or [{}]
        content = str((choices[0].get("message") or {}).get("content") or "")
        return verdict(content)

    return check


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
