"""Pictures she makes on purpose: an illustration drawn from a considered brief.

A picture is a decision, not a reflex. The tool does not take one line; it takes what is in her
head: what the picture is meant to say (``meaning``), what exactly is in it (``scene``), the
style she chose and, when she has them, the mood, the framing and what must not appear. The
prompt for the image model is built from those parts, and every brief is kept in a journal
next to the picture, so it stays visible why she drew it. The result is a JPEG in her own
``art`` folder; she looks at it with the ``look`` tool and draws again when it does not say
what she meant, and a post to her channel may use it (``art/<name>.jpg``) through the same
check as any picture.

Bounds: a daily cap, a gap between pictures, the daily money budget of the harness, a fixed
price per picture in the budget currency, STOP at every step (a picture finished after STOP is
dropped), one picture at a time, one JPEG of at most 8 MB. What the model draws is data.
"""

import asyncio
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness_host import Usage

LOG = logging.getLogger(__name__)
SIZES = {"square": "1024x1024", "wide": "1536x1024", "tall": "1024x1536"}
FIELD_MAX = 700  # characters in one part of the brief
MIN_GAP = 20.0  # seconds between the starts of two pictures
MAX_BYTES = 8 * 1024 * 1024
LABELS = (
    ("scene", ""),
    ("style", "Стиль: "),
    ("mood", "Настроение: "),
    ("composition", "Композиция: "),
    ("meaning", "Что картинка должна выразить: "),
    ("avoid", "Не должно быть на картинке: "),
)
NO_TEXT = (
    "Надписей и букв на картинке не должно быть, кроме тех, что названы в описании сцены в "
    "кавычках."
)

Generate = Callable[[str, str], Awaitable[bytes]]  # prompt, size -> a JPEG


class ImageRefused(Exception):
    """The model declined the brief (its own safety rules): the reason is for her to read."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ImageFailed(Exception):
    """No picture came: a fixed code only."""


def router_image(upstream: str, key: str, model: str) -> Generate:
    """Draw through the provider's image endpoint; the picture comes back as base64 JPEG."""

    def call(prompt: str, size: str) -> bytes:
        body = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": "medium",
            "output_format": "jpeg",
            "output_compression": 85,
        }
        request = urllib.request.Request(
            upstream.rstrip("/") + "/images/generations",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "ai-agent-platform/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                document = json.loads(response.read(32 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            detail = error.read(2000).decode("utf-8", "replace")
            if error.code == 400 and any(
                word in detail.lower() for word in ("safety", "policy", "moderation", "content")
            ):
                raise ImageRefused("модель отказалась рисовать такое") from None
            raise ImageFailed(f"http-{error.code}") from None
        except OSError, ValueError:
            raise ImageFailed("network") from None
        items = document.get("data") if isinstance(document, dict) else None
        data = items[0].get("b64_json") if items and isinstance(items[0], dict) else None
        if not isinstance(data, str):
            raise ImageFailed("no-picture")
        try:
            blob = base64.b64decode(data, validate=False)
        except ValueError:
            raise ImageFailed("bad-picture") from None
        if not blob.startswith(b"\xff\xd8\xff") or len(blob) > MAX_BYTES:
            raise ImageFailed("not-a-jpeg")
        return blob

    async def generate(prompt: str, size: str) -> bytes:
        return await asyncio.to_thread(call, prompt, size)

    return generate


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _slug(name: str) -> str:
    """A plain ASCII file stem (the channel reads only such names)."""
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return stem or "art"


class Imagine:
    def __init__(
        self,
        folder: Path,
        journal: Path,
        generate: Generate,
        *,
        daily: int = 10,
        price: float = 0.05,
        min_gap: float = MIN_GAP,
        usage: Usage | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.folder, self.journal, self.generate = folder, journal, generate
        self.daily, self.price, self.min_gap, self.usage = daily, price, min_gap, usage
        self.clock, self.wall = clock, wall
        self._lock = asyncio.Lock()
        self._last = -1e9

    def made_today(self) -> int:
        today = self.wall().date().isoformat()
        count = 0
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                row = json.loads(line)
                count += row["status"] == "made" and str(row["at"]).startswith(today)
            except ValueError, KeyError, TypeError:
                continue
        return count

    def prompt(self, brief: dict[str, str]) -> str:
        """The prompt of the image model, built from the parts of the brief."""
        lines = [
            f"{prefix}{brief[key]}" + ("" if brief[key].endswith((".", "!", "?")) else ".")
            for key, prefix in LABELS
            if brief.get(key)
        ]
        return "\n".join([*lines, NO_TEXT])

    async def draw(self, arguments: Mapping[str, object], current: Callable[[], None]) -> str:
        brief = {key: _clean(arguments.get(key)) for key, _ in LABELS}
        missing = not (brief["meaning"] and brief["scene"] and brief["style"])
        if missing:
            return (
                "Не нарисовано: опиши замысел до конца. Нужны meaning (зачем эта картинка и что "
                "она должна выразить), scene (что именно на ней) и style (в каком стиле и "
                "почему он подходит)."
            )
        if any(len(value) > FIELD_MAX for value in brief.values()):
            return f"Не нарисовано: каждая часть замысла не длиннее {FIELD_MAX} знаков."
        size = SIZES.get(_clean(arguments.get("size")).lower(), SIZES["wide"])
        async with self._lock:
            current()
            left = self.usage.remaining() if self.usage is not None else None
            if left is not None and left <= 0:
                return "Не рисую: дневной бюджет исчерпан."
            if self.made_today() >= self.daily:
                return f"Не нарисовано: сегодня уже {self.daily} картинок, это дневной лимит."
            if self.clock() - self._last < self.min_gap:
                return "Не нарисовано: слишком часто, подожди полминуты и подумай над замыслом."
            self._last = self.clock()
            try:
                blob = await self.generate(self.prompt(brief), size)
            except ImageRefused as refused:
                self._journal(brief, size, "refused", "", refused.reason)
                return f"Не нарисовано: {refused.reason}. Перепиши замысел или откажись от него."
            except ImageFailed as failed:
                LOG.warning("Image failed: %s", failed)
                self._journal(brief, size, "failed", "", str(failed))
                return "Не нарисовано: картинка не пришла (сбой модели или сети). Попробуй позже."
            current()  # STOP while drawing: the picture is dropped, nothing is saved or paid
            stamp = self.wall().strftime("%Y%m%d-%H%M%S")
            name = f"{stamp}-{_slug(_clean(arguments.get('name')))}.jpg"
            try:
                await asyncio.to_thread(self._save, name, blob)
            except OSError:
                LOG.warning("Picture not saved")
                return "Не нарисовано: не удалось сохранить файл."
            if self.usage is not None:
                self.usage.record_amount(self.price, source="image")
            self._journal(brief, size, "made", name, "")
            return (
                f"Готово: /workspace/art/{name} ({size}). Посмотри, что получилось, инструментом "
                f"look (folder=art, photo={name}): совпало ли с замыслом? Если нет, нарисуй "
                f"заново, изменив описание. Для поста в канал: channel_post с photo=art/{name}."
            )

    def _save(self, name: str, blob: bytes) -> None:
        if self.folder.is_symlink():
            raise OSError("linked folder")
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / name).open("xb") as stream:
            stream.write(blob)

    def _journal(
        self, brief: dict[str, str], size: str, status: str, name: str, detail: str
    ) -> None:
        row = {
            "at": self.wall().isoformat(timespec="seconds"),
            "status": status,
            "file": name,
            "size": size,
            "detail": detail,
            **{key: value for key, value in brief.items() if value},
        }
        try:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            LOG.warning("Image journal not written")
