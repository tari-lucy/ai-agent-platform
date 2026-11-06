"""Local speech recognition (GigaAM v2) for the persona's ears: no money, only this machine.

The recognition itself runs in a small service next to the platform (deploy/asr). Here are the
client for it and what live listening needs in front of it: GigaAM recognises a finished stretch
of speech, not a stream of words, so the audio is cut into phrases by a simple voice detector
(energy against the noise floor) and each phrase is sent once. What comes out has the shape of
the cloud engine's updates (final tokens and an end-of-phrase mark), so the listener does not
care which engine heard. Nothing is stored here, and silence is never sent anywhere.
"""

import asyncio
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from array import array
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import dataclass

from .soniox import SpeechToken, TranscriptUpdate

LOG = logging.getLogger(__name__)
RATE = 16000  # the service's rate
END_MARK = "<end>"
MAX_PCM_SECONDS = 55  # one request; a phrase never gets longer (a hard cut)


class AsrError(Exception):
    """Fixed codes only: no audio and no text in the message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AsrConfig:
    url: str = "http://127.0.0.1:8090"

    def __post_init__(self) -> None:
        host = urllib.parse.urlsplit(self.url).hostname if "//" in self.url else None
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("the recognition service must be on this machine's loopback")


class GigaAm:
    """Client of the local recognition service."""

    def __init__(self, config: AsrConfig | None = None) -> None:
        self.config = config or AsrConfig()

    def _call(self, path: str, data: bytes | None, timeout: float) -> dict[str, object]:
        request = urllib.request.Request(
            self.config.url.rstrip("/") + path,
            data=data,
            method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/octet-stream", "User-Agent": "ai-agent/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                document = json.loads(response.read(2 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            raise AsrError(f"http-{error.code}") from None
        except OSError, ValueError:
            raise AsrError("unavailable") from None
        if not isinstance(document, dict):
            raise AsrError("bad-answer")
        return document

    async def healthy(self) -> bool:
        try:
            document = await asyncio.to_thread(self._call, "/health", None, 3.0)
        except AsrError:
            return False
        return document.get("ok") is True

    async def recognize_pcm(self, pcm: bytes) -> str:
        """The text of 16 kHz mono 16-bit PCM (up to a minute)."""
        document = await asyncio.to_thread(self._call, "/recognize", pcm, 90.0)
        return str(document.get("text") or "")

    async def recognize_file(self, data: bytes) -> str:
        """The text of an audio file (ogg/opus, mp3, wav, m4a, flac, webm)."""
        document = await asyncio.to_thread(self._call, "/recognize-file", data, 300.0)
        return str(document.get("text") or "")


def resample(pcm: bytes, rate: int, target: int = RATE) -> bytes:
    """Linear resampling of 16-bit mono PCM; the same bytes when the rates are equal."""
    if rate == target:
        return pcm
    source = array("h")
    source.frombytes(pcm[: len(pcm) // 2 * 2])
    if not source:
        return b""
    step = rate / target
    count = int(len(source) / step)
    out = array("h")
    for index in range(count):
        position = index * step
        low = int(position)
        high = min(low + 1, len(source) - 1)
        fraction = position - low
        out.append(int(source[low] * (1 - fraction) + source[high] * fraction))
    return out.tobytes()


class Segmenter:
    """Cuts a stream of PCM into phrases: speech starts when the sound stays above the noise floor
    for ``start_ms`` and ends after ``end_ms`` of quiet; a little before the start is kept so the
    first syllable is not lost; a phrase longer than ``max_seconds`` is cut; clicks are dropped."""

    def __init__(
        self,
        rate: int,
        *,
        frame_ms: int = 20,
        start_ms: int = 120,
        end_ms: int = 700,
        preroll_ms: int = 300,
        min_ms: int = 300,
        max_seconds: float = MAX_PCM_SECONDS,
        floor: float = 0.012,
        ratio: float = 3.0,
    ) -> None:
        self.rate, self.floor, self.ratio = rate, floor, ratio
        self.frame_samples = rate * frame_ms // 1000
        self.start_frames = max(1, start_ms // frame_ms)
        self.end_frames = max(1, end_ms // frame_ms)
        self.min_frames = max(1, min_ms // frame_ms)
        self.max_frames = int(max_seconds * 1000 // frame_ms)
        self._preroll: deque[bytes] = deque(maxlen=max(1, preroll_ms // frame_ms))
        self._tail = b""
        self._noise = floor / ratio
        # The room's noise: a low percentile of the last ~10 s of frames. The first half second
        # only calibrates it, so a steady hum is never taken for the first phrase.
        self._recent: deque[float] = deque(maxlen=10_000 // frame_ms)
        self._calibration = 500 // frame_ms
        self._counted = 0
        self._loud = 0
        self._quiet = 0
        self._speech: list[bytes] | None = None

    def _rms(self, frame: bytes) -> float:
        values = array("h")
        values.frombytes(frame)
        return math.sqrt(sum(v * v for v in values) / len(values)) / 32768.0

    def feed(self, chunk: bytes) -> list[bytes]:
        """The phrases finished by this chunk (usually none)."""
        data = self._tail + chunk
        size = self.frame_samples * 2
        usable = len(data) // size * size
        self._tail = data[usable:]
        done: list[bytes] = []
        for start in range(0, usable, size):
            phrase = self._frame(data[start : start + size])
            if phrase is not None:
                done.append(phrase)
        return done

    def flush(self) -> bytes | None:
        """The phrase in progress when the stream ends, if it is long enough."""
        speech, self._speech = self._speech, None
        if speech is not None and len(speech) - self._quiet >= self.min_frames:
            return b"".join(speech)
        return None

    def _frame(self, frame: bytes) -> bytes | None:
        level = self._rms(frame)
        self._recent.append(level)
        self._counted += 1
        if self._counted % 25 == 0 or self._counted == self._calibration:
            self._noise = sorted(self._recent)[len(self._recent) // 5]
        if self._counted <= self._calibration:
            self._preroll.append(frame)
            return None
        loud = level > max(self.floor, self._noise * self.ratio)
        if self._speech is None:
            if loud:
                self._loud += 1
                self._preroll.append(frame)
                if self._loud >= self.start_frames:
                    self._speech = list(self._preroll)
                    self._quiet = 0
            else:
                self._loud = 0
                self._preroll.append(frame)
            return None
        self._speech.append(frame)
        self._quiet = 0 if loud else self._quiet + 1
        if self._quiet >= self.end_frames or len(self._speech) >= self.max_frames:
            speech, quiet = self._speech, self._quiet
            self._speech, self._loud, self._quiet = None, 0, 0
            self._preroll.clear()
            if len(speech) - quiet >= self.min_frames:
                return b"".join(speech)
        return None


def local_transcribe(
    asr: GigaAm, *, floor: float = 0.012
) -> Callable[[AsyncIterable[bytes], int], AsyncGenerator[TranscriptUpdate]]:
    """A ``transcribe`` for the listener: phrases from the capture, text from the local service."""

    async def transcribe(
        chunks: AsyncIterable[bytes], rate: int
    ) -> AsyncGenerator[TranscriptUpdate]:
        segmenter = Segmenter(rate, floor=floor)

        async def say(phrase: bytes) -> TranscriptUpdate | None:
            try:
                text = (await asr.recognize_pcm(resample(phrase, rate))).strip()
            except AsrError as error:
                LOG.warning("Local recognition failed: %s", error.code)
                return None
            if not text:
                return None
            return TranscriptUpdate(
                (SpeechToken(text, True), SpeechToken(END_MARK, True)), False, None, None
            )

        async for chunk in chunks:
            for phrase in segmenter.feed(chunk):
                update = await say(phrase)
                if update is not None:
                    yield update
        tail = segmenter.flush()
        if tail is not None:
            update = await say(tail)
            if update is not None:
                yield update

    return transcribe
