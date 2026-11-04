"""Mouth from real PCM: loudness windows, smoothing, pacing and the clean end."""

import asyncio
import math
from array import array

import pytest
from ai_agent_application.avatar import (
    Avatar,
    ModelInfo,
    ParameterRange,
    Smoother,
    drive,
    profile_from_json,
    window_levels,
)

RATE = 24000
WINDOW = RATE * 60 // 1000  # samples per 60 ms window


def tone(amplitude: float, windows: int) -> bytes:
    samples = array(
        "h",
        (
            int(32767 * amplitude * math.sin(2 * math.pi * 220 * n / RATE))
            for n in range(WINDOW * windows)
        ),
    )
    return samples.tobytes()


def test_levels_follow_loudness_and_silence_is_shut():
    loud, soft, quiet = (window_levels(tone(a, 2), RATE) for a in (0.5, 0.15, 0.01))
    assert len(loud) == 2 and loud[0] == 1.0
    assert 0.0 < soft[0] < 1.0
    assert quiet == [0.0, 0.0]
    assert window_levels(b"\x00" * (WINDOW * 2 - 2), RATE) == []  # a tail is not a frame
    assert window_levels(b"\x01", RATE) == []  # an odd byte is ignored, not an error


def test_smoother_opens_fast_and_closes_slowly():
    smooth = Smoother()
    opened = smooth(1.0)
    assert opened >= 0.7
    first = smooth(0.0)
    assert 0 < first < opened and smooth(0.0) < first
    for _ in range(20):
        smooth(0.0)
    assert smooth(0.0) == 0.0


class Rig:
    def __init__(self):
        self.mouth = []
        self.neutral = 0

    async def connect(self):
        return ModelInfo(
            "T", hotkeys={"Reset": "r"}, parameters={"MouthOpen": ParameterRange(0, 1)}
        )

    async def hotkey(self, hotkey_id):
        self.neutral += 1

    async def expression(self, file, *, active):
        pass

    async def inject(self, values):
        self.mouth.append(values["MouthOpen"])

    async def close(self):
        pass


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def make():
    rig, clock = Rig(), Clock()
    profile = profile_from_json(
        {"version": 1, "neutral": {"hotkey": "Reset"}, "mouth_parameter": "MouthOpen"}
    )
    avatar = Avatar(rig, profile, clock=clock)

    async def sleep(seconds):  # the audio's pace, on the fake clock
        clock.now += seconds

    return avatar, rig, sleep


async def stream(*chunks):
    for chunk in chunks:
        yield chunk


def test_speech_moves_the_mouth_in_step_and_ends_neutral():
    async def scenario():
        avatar, rig, sleep = make()
        pcm = tone(0.5, 3) + tone(0.0, 2)
        # Chunks of an awkward size: windows are cut across chunk edges.
        chunks = [pcm[i : i + 1000] for i in range(0, len(pcm), 1000)]
        await drive(avatar, stream(*chunks), RATE, sleep=sleep)
        frames = rig.mouth[:-1]  # the last one is the closing frame
        assert frames[0] > 0.5 and frames[-1] < frames[0]
        assert rig.mouth[-1] == 0.0 and rig.neutral >= 1

    asyncio.run(scenario())


def test_cancel_and_failing_streams_close_the_mouth():
    async def scenario():
        avatar, rig, sleep = make()

        async def endless():
            while True:
                yield tone(0.5, 1)

        async def slow_sleep(seconds):
            await asyncio.sleep(0.01)
            await sleep(seconds)

        task = asyncio.create_task(drive(avatar, endless(), RATE, sleep=slow_sleep))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.mouth[-1] == 0.0 and rig.neutral >= 1

        avatar, rig, sleep = make()

        async def broken():
            yield tone(0.5, 1)
            raise OSError("stream dropped")

        with pytest.raises(OSError):
            await drive(avatar, broken(), RATE, sleep=sleep)
        assert rig.mouth[-1] == 0.0 and rig.neutral >= 1

    asyncio.run(scenario())


def test_a_stuck_consumer_cannot_grow_the_buffer_forever():
    async def scenario():
        avatar, rig, sleep = make()
        huge = b"\x00\x10" * (RATE * 30)  # 30 s of tone in one chunk
        await drive(avatar, stream(huge), RATE, sleep=sleep)
        assert len(rig.mouth) <= 4 * 1000 // 60 + 3  # only the last ~4 s were ever windowed

    asyncio.run(scenario())
