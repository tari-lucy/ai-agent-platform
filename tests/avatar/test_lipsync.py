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
WINDOW = RATE * 80 // 1000  # samples per 80 ms window


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
        await asyncio.sleep(0)  # other tasks (the connection) get their turn, as in real time

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


def test_a_slow_send_does_not_slow_the_mouth_down():
    """Each frame takes 40 ms to send; 30 windows must still last 30 windows, not 30 + sends."""

    class SlowRig(Rig):
        def __init__(self, clock):
            super().__init__()
            self.clock = clock

        async def inject(self, values):
            self.clock.now += 0.04
            await super().inject(values)

    async def scenario():
        clock = Clock()
        rig = SlowRig(clock)
        profile = profile_from_json(
            {"version": 1, "neutral": {"hotkey": "Reset"}, "mouth_parameter": "MouthOpen"}
        )
        avatar = Avatar(rig, profile, clock=clock)

        async def sleep(seconds):
            clock.now += seconds

        pcm = tone(0.5, 30)  # 30 windows of 80 ms = 2.4 s of speech
        await avatar.ready()
        began = clock.now
        await drive(avatar, stream(pcm), RATE, sleep=sleep, now=clock)
        return clock.now - began

    assert abs(asyncio.run(scenario()) - 2.4) < 0.15


def test_syllables_open_the_mouth_and_dips_between_them_shut_it():
    """Speech that never goes silent, only dips: the mouth must still close in the dips."""
    from ai_agent_application.avatar import Envelope, Smoother

    envelope, smooth = Envelope(), Smoother()
    syllable, dip = 0.14, 0.04  # typical loudness of a voiced window and of a dip (RMS)
    shown = [smooth(envelope(rms)) for _ in range(6) for rms in (syllable, syllable, dip, dip)]
    tail = shown[8:]  # the first syllables only teach the envelope the peak
    assert max(tail) > 0.85 and min(tail) < 0.2


def test_a_quiet_phrase_is_not_blown_up_to_a_wide_mouth():
    from ai_agent_application.avatar import Envelope

    envelope = Envelope()
    assert [envelope(0.01) for _ in range(5)] == [0.0] * 5  # breathing/noise: shut


def test_a_slow_connection_does_not_shift_the_mouth_later_than_the_voice():
    """The program connects half-way through the phrase: earlier frames are skipped, the rest
    stay in step, and the mouth does not run on after the voice has ended."""

    async def scenario():
        clock = Clock()
        gate = asyncio.Event()
        rig = Rig()
        connect = rig.connect

        async def slow_connect():
            await gate.wait()  # the program answers late
            return await connect()

        rig.connect = slow_connect
        profile = profile_from_json(
            {"version": 1, "neutral": {"hotkey": "Reset"}, "mouth_parameter": "MouthOpen"}
        )
        avatar = Avatar(rig, profile, clock=clock)
        windows = 40  # 3.2 s of speech

        async def sleep(seconds):
            clock.now += seconds
            if clock.now - began > 1.2:
                gate.set()  # connected half-way through
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        began = clock.now
        speech = (tone(0.5, 2) + tone(0.0, 2)) * (windows // 4)  # syllables, so frames differ
        await drive(avatar, stream(speech), RATE, sleep=sleep, now=clock)
        return clock.now - began, len(rig.mouth)

    took, frames = asyncio.run(scenario())
    assert abs(took - 3.2) < 0.2  # it ends with the voice, not seconds after it
    assert 5 < frames < 40  # the first half skipped, the second half drawn
