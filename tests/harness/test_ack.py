"""The instant reply: a prepared short phrase plays the moment she is addressed by name."""

import asyncio
import math
from array import array
from types import SimpleNamespace

from ai_agent_core.local_host.harness import (
    DEFAULT_FILLERS,
    FILLERS_TEXT,
    read_fillers,
    seed_fillers,
    wire_ack,
)
from ai_agent_core.m3_composition.harness_listen import Listener, ListenSource
from ai_agent_core.m3_composition.harness_speech import Speaker

RATE = 24000


def window():
    n = RATE * 60 // 1000
    return array("h", (int(32767 * 0.4 * math.sin(i / 4)) for i in range(n))).tobytes()


class Sink:
    def __init__(self):
        self.phrases = []

    async def play(self, chunks):
        got = b""
        async for chunk in chunks:
            got += chunk
        self.phrases.append(len(got))


def speaker(tmp_path, *, synth_calls=None, slow=0.0, fail=False):
    async def synth(text):
        if synth_calls is not None:
            synth_calls.append(text)
        if fail:
            raise OSError("no voice")
        for _ in range(3):
            await asyncio.sleep(slow)
            yield window()

    sink = Sink()
    return Speaker(synth, sink, RATE, tmp_path / "speech.jsonl"), sink


def test_a_prepared_reply_plays_at_once_without_synthesis(tmp_path):
    async def scenario():
        calls = []
        spk, sink = speaker(tmp_path, synth_calls=calls)
        await spk.prepare_acks(["Да, слушаю.", "Мм?"])
        assert calls == ["Да, слушаю.", "Мм?"] and spk.has_ack("Мм?")
        assert spk.ack("Мм?") is True
        await spk._ack_task
        assert sink.phrases == [3 * len(window())]
        assert calls == ["Да, слушаю.", "Мм?"]  # nothing was synthesised for the playing
        assert spk.ack("не готова") is False  # a phrase that was not prepared never plays
        return sink

    asyncio.run(scenario())


def test_a_second_reply_does_not_start_over_a_playing_one_and_a_real_answer_follows_it(tmp_path):
    async def scenario():
        spk, sink = speaker(tmp_path, slow=0.05)
        await spk.prepare_acks(["Мм?"])
        assert spk.ack("Мм?") is True
        assert spk.ack("Мм?") is False  # already playing
        said = await spk.speak("А теперь настоящий ответ.", lambda: None)
        assert said == "Сказано."  # not "I am still speaking": it waited for the short reply
        assert len(sink.phrases) == 2

    asyncio.run(scenario())


def test_a_reply_that_cannot_be_prepared_is_just_not_used(tmp_path):
    async def scenario():
        spk, _ = speaker(tmp_path, fail=True)
        await spk.prepare_acks(["Мм?", "x" * 61, ""])
        assert not spk.has_ack("Мм?") and spk.ack("Мм?") is False

    asyncio.run(scenario())


def listener(tmp_path, clock):
    async def nothing(chunks, rate):
        if False:
            yield None

    return Listener(
        lambda: None,  # type: ignore[arg-type]
        nothing,
        ["Опечатка"],
        tmp_path / "l.jsonl",
        clock=clock,
    )


def test_the_hook_fires_for_a_name_after_a_quiet_spell_only():
    import pathlib
    import tempfile

    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    with tempfile.TemporaryDirectory() as folder:
        clock = Clock()
        ears = listener(pathlib.Path(folder), clock)
        fired = []
        ears.on_addressed = lambda: fired.append(clock.now)
        ears._phrase("привет всем")  # not addressed
        assert fired == []
        ears._phrase("опечатка слышишь")  # addressed after a quiet spell
        assert fired == [1000.0]
        clock.now += 10
        ears._phrase("опечатка ещё раз")  # in the middle of the conversation: no reply
        assert fired == [1000.0]
        clock.now += 200  # the conversation went quiet
        ears._phrase("опечатка ты тут")
        assert fired == [1000.0, 1210.0]


def test_a_failing_hook_never_stops_the_listening(tmp_path):
    ears = listener(tmp_path, lambda: 5.0)

    def broken():
        raise RuntimeError("boom")

    ears.on_addressed = broken
    ears._phrase("опечатка привет")
    assert ears.take()  # the phrase still reached her


def test_the_wake_text_asks_for_one_short_voice_reply():
    async def scenario():
        class Ears:
            def take(self):
                return [("heard-1", "привет")]

        events = await ListenSource(Ears()).poll(None, lambda: None)  # type: ignore[arg-type]
        return events[0].text

    text = asyncio.run(scenario())
    assert "одним коротким speak и закончи ход" in text and "«привет»" in text


def test_her_fillers_come_from_her_file_with_defaults_as_the_fallback(tmp_path):
    assert read_fillers(None) == DEFAULT_FILLERS
    path = tmp_path / "fillers.txt"
    assert read_fillers(path) == DEFAULT_FILLERS  # no file
    path.write_text("# comment\n\nЧто такое?\n" + "x" * 61 + "\nЯ тут.\n", encoding="utf-8")
    assert read_fillers(path) == ["Что такое?", "Я тут."]
    path.write_text("# only a comment\n", encoding="utf-8")
    assert read_fillers(path) == DEFAULT_FILLERS


def test_the_fillers_file_is_put_once_and_stays_hers(tmp_path):
    assert seed_fillers(tmp_path, "p") is True
    target = tmp_path / "p" / "playbook" / "fillers.txt"
    assert target.read_text(encoding="utf-8") == FILLERS_TEXT
    assert read_fillers(target) == DEFAULT_FILLERS  # the seeded file reads as the defaults
    target.write_text("Погоди.\n", encoding="utf-8")
    assert seed_fillers(tmp_path, "p") is False
    assert read_fillers(target) == ["Погоди."]


def test_the_wiring_prepares_on_start_replies_once_per_gap_and_turns_the_face():
    async def scenario():
        class Spk:
            def __init__(self):
                self.prepared, self.played = [], []
                self.ready = {"Мм?", "Секунду."}

            async def prepare_acks(self, texts):
                self.prepared.append(list(texts))

            def has_ack(self, text):
                return text in self.ready

            def ack(self, text):
                self.played.append(text)
                return True

        class Face:
            def __init__(self):
                self.shown = []

            async def express(self, emotion):
                self.shown.append(emotion.value)

        class Ears:
            on_start = on_addressed = None

        clock = {"now": 100.0}
        ears, spk, face = Ears(), Spk(), Face()
        wire_ack(ears, spk, face, None, gap=20.0, clock=lambda: clock["now"])  # type: ignore[arg-type]
        ears.on_start()
        await asyncio.sleep(0)
        assert spk.prepared == [DEFAULT_FILLERS]
        ears.on_addressed()
        ears.on_addressed()  # inside the gap: nothing
        await asyncio.sleep(0)
        assert len(spk.played) == 1 and spk.played[0] in spk.ready and face.shown == ["thinking"]
        clock["now"] += 25
        ears.on_addressed()
        assert len(spk.played) == 2 and spk.played[1] != spk.played[0]  # not the same twice

    asyncio.run(scenario())


def test_nothing_plays_when_nothing_is_ready():
    class Spk:
        def has_ack(self, text):
            return False

        def ack(self, text):  # pragma: no cover - must not be called
            raise AssertionError("played something that was not ready")

    ears = SimpleNamespace(on_start=None, on_addressed=None)

    async def scenario():
        wire_ack(ears, Spk(), None, None)  # type: ignore[arg-type]
        ears.on_addressed()

    asyncio.run(scenario())
