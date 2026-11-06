"""Local recognition: phrases cut from a stream, the client of the service, the listener's shape."""

import asyncio
import importlib.util
import math
import threading
from array import array
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from ai_agent_adapters.voice.gigaam import (
    END_MARK,
    AsrConfig,
    AsrError,
    GigaAm,
    Segmenter,
    local_transcribe,
    resample,
)

SERVER = Path(__file__).resolve().parents[2] / "deploy" / "asr" / "gigaam_server.py"
SPEC = importlib.util.spec_from_file_location("gigaam_server_for_client", SERVER)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)

RATE = 16000


def tone(seconds, amplitude=0.3, rate=RATE):
    count = int(seconds * rate)
    return array(
        "h", (int(32767 * amplitude * math.sin(2 * math.pi * 220 * n / rate)) for n in range(count))
    ).tobytes()


def silence(seconds, rate=RATE):
    return b"\x00\x00" * int(seconds * rate)


def cut(segmenter, *pieces, size=3200):
    """Feed the pieces in chunks of ``size`` bytes (awkward, not frame-aligned) -> the phrases."""
    stream = b"".join(pieces)
    phrases = []
    for start in range(0, len(stream), size):
        phrases += segmenter.feed(stream[start : start + size])
    return phrases


def test_a_phrase_is_cut_out_of_silence_and_a_click_is_dropped():
    segmenter = Segmenter(RATE)
    phrases = cut(
        segmenter,
        silence(1.0),
        tone(0.04),  # a click: shorter than the start threshold
        silence(1.0),
        tone(1.5),  # a phrase
        silence(1.2),
        tone(0.8),  # another one
        silence(1.2),
    )
    assert len(phrases) == 2
    first, second = (len(p) / (RATE * 2) for p in phrases)
    assert 1.4 < first < 2.5 and 0.7 < second < 1.8


def test_the_beginning_of_a_phrase_is_kept_and_silence_alone_gives_nothing():
    assert cut(Segmenter(RATE), silence(5.0)) == []
    phrases = cut(Segmenter(RATE), silence(1.0), tone(1.0), silence(1.0))
    assert len(phrases) == 1
    assert len(phrases[0]) / (RATE * 2) >= 1.2  # the tone plus the kept lead-in and trailing quiet


def test_a_very_long_speech_is_cut():
    segmenter = Segmenter(RATE, max_seconds=2.0)
    phrases = cut(segmenter, silence(1.0), tone(3.5), silence(1.2))
    assert len(phrases) >= 2 and all(len(p) / (RATE * 2) <= 2.1 for p in phrases)


def test_the_end_of_the_stream_flushes_the_phrase_in_progress():
    segmenter = Segmenter(RATE)
    assert cut(segmenter, silence(1.0), tone(1.0)) == []  # no quiet yet: the phrase is open
    tail = segmenter.flush()
    assert tail is not None and len(tail) / (RATE * 2) > 0.8
    assert segmenter.flush() is None and Segmenter(RATE).flush() is None


def test_a_steady_noise_is_not_speech_but_a_voice_over_it_is():
    noise = array("h", (int(32767 * 0.02 * math.sin(n * 1.3)) for n in range(RATE * 3))).tobytes()
    assert cut(Segmenter(RATE), noise) == []
    voice = tone(1.0, amplitude=0.4)
    assert len(cut(Segmenter(RATE), noise, voice, noise)) == 1


def test_resampling_changes_the_length_and_keeps_equal_rates_as_they_are():
    audio = tone(1.0, rate=48000)
    out = resample(audio, 48000)
    assert len(out) == len(audio) // 3 + len(audio) % 2 or abs(len(out) - 32000) <= 2
    assert resample(tone(1.0), RATE) == tone(1.0)
    assert resample(b"", 8000) == b""
    assert abs(len(resample(tone(1.0, rate=8000), 8000)) - 32000) <= 2


class Fake:
    def __init__(self, text="привет опечатка"):
        self.text, self.seen = text, []

    def __call__(self, samples):
        self.seen.append(len(samples))
        return self.text


def serve(recognize):
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(recognize, lambda: True))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, GigaAm(AsrConfig(f"http://127.0.0.1:{server.server_address[1]}"))


def test_the_client_talks_to_the_service_and_reports_failures_with_fixed_codes():
    async def scenario():
        fake = Fake()
        server, asr = serve(fake)
        try:
            assert await asr.healthy() is True
            assert await asr.recognize_pcm(tone(1.0)) == "привет опечатка"
            assert fake.seen == [RATE]
            with pytest.raises(AsrError) as error:
                await asr.recognize_pcm(tone(61.0))
            assert error.value.code == "http-413"
        finally:
            server.shutdown()
            server.server_close()
        assert await asr.healthy() is False
        with pytest.raises(AsrError) as error:
            await asr.recognize_pcm(tone(1.0))
        assert error.value.code == "unavailable"

    asyncio.run(scenario())


def test_the_service_must_be_on_this_machine():
    with pytest.raises(ValueError):
        AsrConfig("http://203.0.113.5:8090")
    with pytest.raises(ValueError):
        AsrConfig("not a url")
    assert AsrConfig("http://127.0.0.1:8090")


def test_the_listener_gets_updates_with_final_text_and_an_end_mark():
    async def scenario():
        fake = Fake("слушай опечатка")
        server, asr = serve(fake)

        async def capture():
            for piece in (silence(1.0), tone(1.2), silence(1.2), tone(0.8), silence(1.2)):
                for start in range(0, len(piece), 3200):
                    yield piece[start : start + 3200]

        try:
            updates = [u async for u in local_transcribe(asr)(capture(), RATE)]
        finally:
            server.shutdown()
            server.server_close()
        return updates

    updates = asyncio.run(scenario())
    assert len(updates) == 2
    for update in updates:
        assert [t.text for t in update.tokens] == ["слушай опечатка", END_MARK]
        assert all(t.is_final for t in update.tokens)


def test_a_failing_service_loses_a_phrase_not_the_listening():
    async def scenario():
        server, asr = serve(Fake())
        server.shutdown()  # gone before the first phrase
        server.server_close()

        async def capture():
            for piece in (silence(0.5), tone(1.0), silence(1.0)):
                yield piece

        return [u async for u in local_transcribe(asr)(capture(), RATE)]

    assert asyncio.run(scenario()) == []
