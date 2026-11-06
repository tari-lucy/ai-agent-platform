"""Mouth from the audio that is really playing (doc04, doc37 A2): loudness windows, paced in time.

The level of each short window of speech (not the moment the text was generated) moves the mouth:
fast to open, slower to close, so it reads as speech and not as a flicker. The pace is the
audio's own: one window of samples takes one window of time. Cancel the task (STOP, an
interrupted phrase, a dropped connection) and the mouth is closed and the face is neutral again.
Amplitude only; visemes need a TTS and a rig that supply them.
"""

import asyncio
import math
import time
from array import array
from collections.abc import AsyncIterable, Awaitable, Callable
from contextlib import suppress

from .service import Avatar

WINDOW_MS = 60  # one mouth frame per window (~16 Hz, inside the avatar's own 20 Hz limit)
REFERENCE = 0.22  # RMS (of full scale) that opens the mouth fully; speech sits around 0.05-0.25
FLOOR = 0.03  # below this RMS it is breathing/noise: the mouth stays shut
CONTRAST = 1.5  # >1: quiet stretches between syllables close the mouth more (fixed scale)
PEAK_MIN = 0.08  # the peak never falls below this: a quiet phrase is not blown up to full
PEAK_DECAY = 0.97  # per window: the remembered peak fades with a half-life of about 1.2 s
LOW = 0.35  # a window quieter than this share of the peak keeps the mouth shut
HIGH = 0.8  # and this share opens it fully
ATTACK = 0.8  # share of the way to a louder level covered in one window
RELEASE = 0.6  # the same going down
STALL = 0.25  # seconds: a gap in the stream this long starts the schedule again
MAX_BUFFER = 24000 * 2 * 4  # bytes of samples waiting for a window: a stuck consumer is bounded


def window_rms(pcm: bytes, rate: int, *, window_ms: int = WINDOW_MS) -> list[float]:
    """RMS (0..1 of full scale) of each full window of 16-bit little-endian mono PCM."""
    size = rate * window_ms // 1000
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if samples.itemsize != 2:  # pragma: no cover - 'h' is 2 bytes on every supported platform
        raise ValueError("array('h') is not 16-bit")
    return [
        math.sqrt(sum(s * s for s in samples[start : start + size]) / size) / 32768.0
        for start in range(0, len(samples) - size + 1, size)
    ]


def window_levels(pcm: bytes, rate: int, *, window_ms: int = WINDOW_MS) -> list[float]:
    """Loudness 0..1 of each full window, on a fixed scale; a tail is dropped."""
    return [_scale(rms) for rms in window_rms(pcm, rate, window_ms=window_ms)]


def _scale(rms: float) -> float:
    if rms < FLOOR:
        return 0.0
    return float(min((rms - FLOOR) / (REFERENCE - FLOOR), 1.0) ** CONTRAST)


class Envelope:
    """Mouth opening from loudness relative to the phrase's own recent peak.

    Synthesised speech does not fall silent between syllables, only dips, so a fixed scale
    leaves the mouth half open all the time. Compared with the recent peak, a dip below
    ``LOW`` of it shuts the mouth and a syllable near the peak opens it fully.
    """

    def __init__(self) -> None:
        self.peak = PEAK_MIN

    def __call__(self, rms: float) -> float:
        self.peak = max(self.peak * PEAK_DECAY, rms, PEAK_MIN)
        if rms < FLOOR:
            return 0.0
        return min(max((rms / self.peak - LOW) / (HIGH - LOW), 0.0), 1.0)


class Smoother:
    """Attack/release smoothing of the level between frames."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self, level: float) -> float:
        rate = ATTACK if level > self.value else RELEASE
        self.value += (level - self.value) * rate
        return self.value if self.value > 0.01 else 0.0


async def drive(
    avatar: Avatar,
    chunks: AsyncIterable[bytes],
    rate: int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    window_ms: int = WINDOW_MS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Move the mouth along the PCM chunks as they arrive, one window per window of time.

    Call this next to the code that plays the same chunks. It never raises for avatar problems,
    and on any end (finished, cancelled, a failing stream) the mouth is closed and the face is
    neutral again.

    Frames follow the clock, not the sum of pauses: sending a frame takes time (the network to
    the program), and a pause of a full window after it would run the mouth slower than the
    voice and let the lag grow through a phrase. A frame that is a few windows late is dropped.
    """
    smooth, envelope, pending = Smoother(), Envelope(), b""
    step, start, index = window_ms / 1000, 0.0, 0
    await avatar.ready()  # speech may be the first thing she does: connect before the first frame
    size = rate * window_ms // 1000 * 2
    try:
        async for chunk in chunks:
            pending += chunk
            if len(pending) > MAX_BUFFER:
                pending = pending[-MAX_BUFFER:]
            usable = len(pending) // size * size
            data, pending = pending[:usable], pending[usable:]
            if index == 0 or now() - (start + index * step) > STALL:
                start, index = now(), 0  # the first sound, or a stall: do not rush to catch up
            for rms in window_rms(data, rate, window_ms=window_ms):
                due = start + index * step
                index += 1
                value = smooth(envelope(rms))
                if now() - due <= 2 * step:  # a frame much too late is skipped, not sent
                    await avatar.mouth(value)
                await sleep(max(start + index * step - now(), 0.0))
    finally:
        # Also when cancelled: shielded, because the cleanup itself must not be cancelled.
        with suppress(Exception):
            await asyncio.shield(avatar.neutral())
