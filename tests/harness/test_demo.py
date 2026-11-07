"""The demo script: its lines are understood, played in order, and failures are plain notes."""

import asyncio
import math
from array import array
from types import SimpleNamespace

import pytest
from ai_agent_application.avatar import Emotion

from ai_agent_core.local_host.demo_cli import ScriptError, Step, parse_script, play
from ai_agent_core.m3_composition.harness_speech import Speaker

SCRIPT = """\
# a demo
[радость] Привет! Меня зовут Опечатка.
[Смущение]
Просто фраза.
[пауза 1,5]
[sad] Уже грустно.
[пауза 2] А это после паузы.
"""


def test_the_lines_become_steps_in_order():
    steps = parse_script(SCRIPT)
    assert steps == [
        Step(Emotion.HAPPY, "Привет! Меня зовут Опечатка."),
        Step(Emotion.EMBARRASSED, ""),
        Step(None, "Просто фраза."),
        Step(pause=1.5),
        Step(Emotion.SAD, "Уже грустно."),
        Step(pause=2.0),
        Step(None, "А это после паузы."),
    ]


def test_an_unknown_emotion_or_a_huge_line_names_the_line():
    with pytest.raises(ScriptError) as error:
        parse_script("привет\n[ярость] что-то")
    assert "строка 2" in str(error.value) and "ярость" in str(error.value)
    with pytest.raises(ScriptError) as error:
        parse_script("x" * 601)
    assert "строка 1" in str(error.value)
    with pytest.raises(ScriptError):
        parse_script("\n".join(["фраза"] * 201))
    assert parse_script("") == [] and parse_script("# only a comment\n\n") == []


class Face:
    def __init__(self, refuse=()):
        self.shown, self.refuse = [], set(refuse)

    async def express(self, emotion):
        self.shown.append(emotion)
        if emotion in self.refuse:
            return SimpleNamespace(
                applied=False, text=f"Для {emotion.value} в этой модели нет выражения."
            )
        return SimpleNamespace(applied=True, text="ok")


class Voice:
    def __init__(self, fail_on=None):
        self.said, self.prepared_for, self.fail_on = [], [], fail_on

    async def prepare_acks(self, texts, limit=60):
        self.prepared_for.append((list(texts), limit))

    def prepared(self, text):
        return f"stored:{text}"

    async def speak(self, text, current, synth=None):
        assert synth == f"stored:{text}"  # the line plays from what was prepared
        self.said.append(text)
        return "Не получилось." if text == self.fail_on else "Сказано."


def run_play(steps, face, voice, **kwargs):
    log, clock = [], {"now": 0.0}

    async def sleep(seconds):
        clock["now"] += seconds
        log.append(round(seconds, 2))

    async def scenario():
        return await play(
            steps,
            face,
            voice,
            countdown=kwargs.get("countdown", 2),
            pause=0.5,
            out=lambda line: log.append(line),
            sleep=sleep,
            clock=lambda: clock["now"],
        )

    return asyncio.run(scenario()), log


def test_the_script_is_played_with_a_countdown_prepared_lines_and_paced_faces():
    face, voice = Face(), Voice()
    ok, log = run_play(parse_script(SCRIPT), face, voice)
    assert ok is True
    assert voice.said == [
        "Привет! Меня зовут Опечатка.",
        "Просто фраза.",
        "Уже грустно.",
        "А это после паузы.",
    ]
    assert voice.prepared_for == [(voice.said, 600)]  # all lines are prepared before the start
    assert face.shown == [Emotion.HAPPY, Emotion.EMBARRASSED, Emotion.SAD]
    texts = [x for x in log if isinstance(x, str)]
    assert texts[0].startswith("Готовлю озвучку (4 фраз")
    assert texts[1:4] == ["Старт через 2…", "Старт через 1…", "Поехали."]
    # the second face change waits for the avatar's own pacing (1.6 s since the first)
    assert any(isinstance(x, float) and x >= 1.0 for x in log)


def test_a_failed_line_stops_the_script_with_a_note():
    ok, log = run_play(parse_script("Первая.\nВторая.\nТретья."), Face(), Voice(fail_on="Вторая."))
    assert ok is False
    assert any("Не вышло: Не получилось." in x for x in log if isinstance(x, str))
    assert not any("Третья." in x for x in log if isinstance(x, str))


def test_an_emotion_the_model_lacks_is_a_note_not_a_stop():
    face = Face(refuse=[Emotion.THINKING])
    ok, log = run_play(parse_script("[раздумье] Хм.\nДальше."), face, Voice())
    assert ok is True
    assert any("нет выражения" in x for x in log if isinstance(x, str))


def test_a_script_without_an_avatar_is_still_spoken():
    ok, log = run_play(parse_script("[радость] Только голос."), None, Voice())
    assert ok is True


RATE = 24000


def window():
    n = RATE * 60 // 1000
    return array("h", (int(32767 * 0.4 * math.sin(i / 4)) for i in range(n))).tobytes()


def test_the_speaker_plays_a_prepared_long_line_with_no_synthesis(tmp_path):
    async def scenario():
        calls = []

        async def synth(text):
            calls.append(text)
            for _ in range(4):
                yield window()

        played = []

        class Sink:
            async def play(self, chunks):
                total = b""
                async for chunk in chunks:
                    total += chunk
                played.append(len(total))

        speaker = Speaker(synth, Sink(), RATE, tmp_path / "s.jsonl")
        long_line = " ".join(
            ["Это длинная строка сценария."] * 6
        )  # over the 60 characters of a short reply
        await speaker.prepare_acks([long_line], limit=600)
        assert calls == [long_line] and speaker.prepared("нет такой") is None
        said = await speaker.speak(long_line, lambda: None, speaker.prepared(long_line))
        assert said == "Сказано." and played == [4 * len(window())]
        assert calls == [long_line]  # nothing was synthesised for the playing
        await speaker.prepare_acks([long_line], limit=60)  # the short-reply limit still holds
        assert speaker.has_ack(long_line)

    asyncio.run(scenario())
