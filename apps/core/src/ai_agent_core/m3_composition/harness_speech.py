"""Live speech of the persona (doc34 V2, doc37 A2): one phrase, spoken and shown at once.

The text is synthesised to PCM once. The same stream goes to the audio output (the virtual
microphone on the operator's computer, which OBS and the call program take) and to the avatar's
mouth, so the mouth follows what is really heard. Rules (D86, operator, 2026-10-29): live speech
has no line-by-line moderation; what protects it is the topic list in her description, this
journal, the daily limit and STOP. STOP (or any epoch change) cancels both consumers at once:
the output is aborted, the mouth is closed. One phrase at a time; a second call while she is
speaking is refused, not queued. The journal keeps sizes and outcomes, never audio; the text is
stored because it is her own public speech and the operator reads it back.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ai_agent_application.avatar import Avatar, drive
from ai_agent_contracts.codec import ContractError

if TYPE_CHECKING:
    from .harness_host import Usage

LOG = logging.getLogger(__name__)
MAX_TEXT = 600
QUEUE = 64  # chunks the synthesis may run ahead of playback (it is faster than real time)
STUCK = 15.0  # seconds a consumer may refuse a chunk before the phrase is cut
WATCH = 0.1  # seconds between two looks at STOP while a phrase plays
ECHO_TAIL = 3.0  # seconds after her own voice ends in which what is heard may still be her


class Sink(Protocol):
    async def play(self, chunks: AsyncIterable[bytes]) -> None: ...


Synth = Callable[[str], AsyncIterator[bytes]]


class Speaker:
    def __init__(
        self,
        synth: Synth,
        sink: Sink,
        rate: int,
        journal: Path,
        *,
        avatar: Avatar | None = None,
        daily_chars: int = 20000,
        stuck_after: float = STUCK,
        mouth_delay: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
        usage: Usage | None = None,
        hour_rate: float = 0.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.synth, self.sink, self.rate, self.journal = synth, sink, rate, journal
        self.avatar, self.daily_chars, self.clock = avatar, daily_chars, clock
        self.stuck_after = stuck_after
        self.mouth_delay = mouth_delay  # the sound takes the network and the device to be heard
        self.usage, self.hour_rate = usage, hour_rate  # price of one hour of synthesised audio
        self._busy = False
        self.monotonic = monotonic
        self._spoke_at = -1e9

    @property
    def speaking(self) -> bool:
        """Her own voice is playing, or has just stopped: what the ears catch may be herself."""
        return self._busy or self.monotonic() - self._spoke_at < ECHO_TAIL

    def spoken_today(self) -> int:
        """Characters spoken today (UTC day), from the local journal."""
        today = self.clock().date().isoformat()
        total = 0
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                row = json.loads(line)
                if str(row["at"]).startswith(today) and row.get("status") == "spoken":
                    total += int(row["chars"])
            except ValueError, KeyError, TypeError:
                continue
        return total

    async def speak(self, text: str, current: Callable[[], None]) -> str:
        """Say ``text`` now; the returned line is what she is told. ``current`` raises on STOP."""
        text = " ".join(text.split())
        if not text:
            return "Нечего сказать: текст пуст."
        if len(text) > MAX_TEXT:
            return f"Слишком длинно: до {MAX_TEXT} символов за раз, разбей на фразы."
        if self._busy:
            return "Я ещё говорю предыдущую фразу."
        left = self.usage.remaining() if self.usage is not None else None
        if left is not None and left <= 0:
            return "Не говорю: дневной бюджет исчерпан."
        if self.spoken_today() + len(text) > self.daily_chars:
            return "Суточный лимит речи исчерпан: до завтра пиши, а не говори."
        self._busy = True
        try:
            current()
            return await self._say(text, current)
        finally:
            self._busy = False
            self._spoke_at = self.monotonic()

    async def _say(self, text: str, current: Callable[[], None]) -> str:
        # One queue per consumer that exists: an unread queue would fill up and cut the phrase.
        queues: list[asyncio.Queue[bytes | None]] = [
            asyncio.Queue(QUEUE) for _ in range(2 if self.avatar is not None else 1)
        ]

        made = 0  # bytes of audio synthesised: this is what is paid for, heard or not

        async def pump() -> None:
            nonlocal made
            async for chunk in self.synth(text):
                made += len(chunk)
                current()  # STOP between chunks: nothing more is released
                for queue in queues:
                    # The synthesis is faster than real time, playback and the mouth are not: a
                    # full queue just holds the producer back. Only a consumer that takes nothing
                    # for ``stuck_after`` seconds is stuck, and then the phrase is cut.
                    await asyncio.wait_for(queue.put(chunk), self.stuck_after)
            # Only a phrase that really ended says so; on STOP or an error the group cancels the
            # consumers instead, so the output is aborted and not played out.
            for queue in queues:
                await asyncio.wait_for(queue.put(None), self.stuck_after)

        async def mouth() -> None:
            assert self.avatar is not None
            await asyncio.sleep(self.mouth_delay)
            await drive(self.avatar, drain(queues[1]), self.rate)

        async def watch() -> None:
            """STOP is noticed while the buffered audio plays too, not only while synthesising."""
            while True:
                await asyncio.sleep(WATCH)
                current()

        async def drain(queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
            while (chunk := await queue.get()) is not None:
                yield chunk

        status, detail = "interrupted", ""  # until the phrase really ends
        stopped: ContractError | None = None
        try:
            async with asyncio.TaskGroup() as group:
                work = [
                    group.create_task(pump()),
                    group.create_task(self.sink.play(drain(queues[0]))),
                ]
                if self.avatar is not None:
                    work.append(group.create_task(mouth()))
                guard = group.create_task(watch())

                def finished(_: object) -> None:
                    if all(task.done() for task in work):
                        guard.cancel()  # the phrase is over: nothing left to watch

                for task in work:
                    task.add_done_callback(finished)
            status = "spoken"
        except ExceptionGroup as failure:
            errors = failure.exceptions
            stopped = next((e for e in errors if isinstance(e, ContractError)), None)
            if stopped is not None:
                status = "stopped"
            elif any(isinstance(e, TimeoutError) for e in errors):
                status, detail = "cut", "slow consumer"
            else:
                status = "failed"
                detail = ",".join(sorted({getattr(e, "code", type(e).__name__) for e in errors}))
        finally:
            seconds = made / (self.rate * 2)
            if self.usage is not None:
                self.usage.record_amount(seconds / 3600 * self.hour_rate, source="speech")
            self._journal(text, status, detail, seconds)
        if stopped is not None:
            raise stopped  # STOP is the run's, not a message to the persona
        if status == "spoken":
            return "Сказано."
        if status == "cut":
            return "Речь оборвалась: приёмник звука не успевал."
        return f"Не удалось сказать ({detail}). Звук на компьютере может быть не подключён."

    def _journal(self, text: str, status: str, detail: str, seconds: float) -> None:
        row = {
            "at": self.clock().isoformat(timespec="seconds"),
            "status": status,
            "chars": len(text),
            "seconds": round(seconds, 2),
            "text": text,
        }
        if detail:
            row["detail"] = detail
        try:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            LOG.warning("Speech journal not written")
