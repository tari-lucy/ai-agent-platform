"""The fast voice brain: it answers by voice, hands actions to the slow mind, never twice."""

import asyncio
from pathlib import Path

from ai_agent_core.m3_composition.harness_brain import VoiceBrain, parse, take_sentences
from ai_agent_core.m3_composition.harness_listen import ListenSource


def test_parse_splits_speech_and_task() -> None:
    assert parse("Сейчас гляну.\nДЕЛО: найди алмазы") == ("Сейчас гляну.", "найди алмазы")
    assert parse("**Привет**, как дела?") == ("Привет, как дела?", "")
    assert parse("-") == ("", "")
    # the marker in the same line: the task is never spoken
    assert parse("Дай подумать. ДЕЛО: поднять архив") == ("Дай подумать.", "поднять архив")
    assert parse("Иду.\nДЕЛО: узнай\nещё") == ("Иду.", "узнай ещё")


def make(tmp_path: Path, reply: str, fail: bool = False):
    said: list[str] = []
    seen: list[list[dict[str, str]]] = []

    async def ask(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        if fail:
            raise TimeoutError
        return reply

    async def speak(text: str) -> None:
        said.append(text)

    async def core() -> str:
        return "Ты Опечатка."

    return VoiceBrain(ask, speak, core, tmp_path / "talk.log"), said, seen


async def settle(brain: VoiceBrain) -> None:
    await asyncio.gather(*list(brain._tasks))


def test_chat_is_answered_without_waking_the_slow_mind(tmp_path: Path) -> None:
    asyncio.run(_test_chat_is_answered_without_waking_the_slow_mind(tmp_path))


async def _test_chat_is_answered_without_waking_the_slow_mind(tmp_path: Path) -> None:
    brain, said, seen = make(tmp_path, "Всё хорошо, а у тебя?")
    brain.hear("Опечатка, как дела?")
    await settle(brain)
    assert said == ["Всё хорошо, а у тебя?"]
    assert brain.take() == []
    assert "Ты Опечатка." in seen[0][0]["content"]
    assert "Я: Всё хорошо" in (tmp_path / "talk.log").read_text(encoding="utf-8")


def test_action_is_handed_over_with_what_was_said(tmp_path: Path) -> None:
    asyncio.run(_test_action_is_handed_over_with_what_was_said(tmp_path))


async def _test_action_is_handed_over_with_what_was_said(tmp_path: Path) -> None:
    brain, said, seen = make(tmp_path, "Секунду, иду.\nДЕЛО: подойди к игроку")
    brain.hear("Опечатка, подойди ко мне")
    await settle(brain)
    assert said == ["Секунду, иду."]
    ((_, text),) = brain.take()
    assert "Не повторяй" in text and "подойди к игроку" in text
    brain.hear("Опечатка, ты где?")
    await settle(brain)
    assert "подойди к игроку" in seen[1][0]["content"]  # the fast voice knows what is going on
    assert "Человек: Опечатка, подойди ко мне" in seen[1][1]["content"]


def test_failed_model_falls_back_to_the_slow_mind(tmp_path: Path) -> None:
    asyncio.run(_test_failed_model_falls_back_to_the_slow_mind(tmp_path))


async def _test_failed_model_falls_back_to_the_slow_mind(tmp_path: Path) -> None:
    brain, said, _ = make(tmp_path, "", fail=True)
    brain.hear("Опечатка, привет")
    await settle(brain)
    assert said == []
    ((_, text),) = brain.take()
    assert "Быстрый голос не ответил" in text


def test_listen_source_gives_only_handovers(tmp_path: Path) -> None:
    asyncio.run(_test_listen_source_gives_only_handovers(tmp_path))


async def _test_listen_source_gives_only_handovers(tmp_path: Path) -> None:
    brain, _, _ = make(tmp_path, "Ок.\nДЕЛО: сделай")
    brain.hear("Опечатка, сделай")
    await settle(brain)
    events = await ListenSource(object(), brain=brain).poll(None, lambda: None)  # type: ignore[arg-type]
    assert [e.kind for e in events] == ["talk"]


def test_findings_are_woven_into_a_live_talk(tmp_path: Path) -> None:
    asyncio.run(_findings(tmp_path))


async def _findings(tmp_path: Path) -> None:
    now = [100.0]
    said: list[str] = []
    seen: list[list[dict[str, str]]] = []

    async def ask(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "Нашла: у нас ровно три шахты." if len(seen) > 1 else "Секунду.\nДЕЛО: узнай"

    async def speak(text: str) -> None:
        said.append(text)

    async def core() -> str:
        return "Ты Опечатка."

    brain = VoiceBrain(ask, speak, core, tmp_path / "talk.log", clock=lambda: now[0])
    brain.hear("Опечатка, сколько тут шахт?")
    await settle(brain)
    assert "voice_brief" in brain.take()[0][1]
    now[0] += 30  # a live talk: the voice brings the finding up itself
    assert "вплетёт" in brain.brief("шахт три: A, B, C")
    await settle(brain)
    assert said == ["Секунду.", "Нашла: у нас ровно три шахты."]
    assert "шахт три" in seen[1][0]["content"] and "Медленный ум" in seen[1][1]["content"]
    assert brain.take() == []  # a finding never wakes the slow mind again
    now[0] += 300  # a task is still awaited (slow mind took minutes): the finding is said
    brain.briefs.clear()
    assert "вплетёт" in brain.brief("позднее, но ждали")
    await settle(brain)
    assert len(said) == 3
    now[0] += 600  # nobody talks, nothing awaited: only remembered, not said out of the blue
    assert "знает" in brain.brief("ещё факт", now=True)
    assert len(said) == 3 and "ещё факт" in " ".join(brain.briefs)
    assert "Нечего" in brain.brief("  ")


def test_say_drop_feeds_the_voice_and_removes_the_file(tmp_path: Path) -> None:
    from ai_agent_core.m3_composition.harness_brain import SayDrop

    async def scenario() -> None:
        brain, said, _ = make(tmp_path, "Привет-привет.")
        folder = tmp_path / "say"
        folder.mkdir()
        (folder / "a.say").write_text("Опечатка,   привет", encoding="utf-8")
        SayDrop(brain, folder, every=0).tick()
        await settle(brain)
        assert said == ["Привет-привет."]
        assert not list(folder.glob("*.say"))

    asyncio.run(scenario())


def test_old_findings_and_talk_are_forgotten(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = [0.0]
        seen: list[list[dict[str, str]]] = []

        async def ask(messages: list[dict[str, str]]) -> str:
            seen.append(messages)
            return "Ок."

        async def speak(text: str) -> None:
            return None

        async def core() -> str:
            return "Ты Опечатка."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", clock=lambda: now[0])
        brain.brief("цены на билеты 300-1500", now=False)
        brain.hear("Опечатка, привет")
        await settle(brain)
        assert "цены на билеты" in seen[0][0]["content"]
        now[0] = 1000.0  # a new talk much later: the old finding and turns are gone
        brain.hear("Опечатка, где ты была?")
        await settle(brain)
        assert "цены на билеты" not in seen[1][0]["content"]
        assert "привет" not in seen[1][1]["content"]

    asyncio.run(scenario())


def test_a_fact_question_is_always_handed_over(tmp_path: Path) -> None:
    async def scenario() -> None:
        brain, said, _ = make(tmp_path, "Да где только не была!")  # the model forgot ДЕЛО
        brain.hear("Опечатка, а где ты уже была в квадратном мире?")
        await settle(brain)
        assert said == ["Да где только не была!"]
        ((_, text),) = brain.take()
        assert "Не повторяй" in text and "записи и память" in text
        brain.hear("Опечатка, ты любишь кофе?")  # plain talk: no hand-over
        await settle(brain)
        assert brain.take() == []

    asyncio.run(scenario())


def test_a_cut_off_answer_is_asked_again_or_trimmed(tmp_path: Path) -> None:
    async def scenario() -> None:
        replies = iter(["Всегда пожалуйста. Если вдруг Глав", "Всегда пожалуйста. Если вдруг Глав"])
        said: list[str] = []

        async def ask(messages: list[dict[str, str]]) -> str:
            return next(replies)

        async def speak(text: str) -> None:
            said.append(text)

        async def core() -> str:
            return "Ты Опечатка."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log")
        brain.hear("Опечатка, спасибо")
        await settle(brain)
        assert said == ["Всегда пожалуйста."]  # the cut tail is not spoken

    asyncio.run(scenario())


def test_memory_is_read_for_the_voice_and_the_slow_mind_still_checks(tmp_path: Path) -> None:
    async def scenario() -> None:
        seen: list[list[dict[str, str]]] = []

        async def ask(messages: list[dict[str, str]]) -> str:
            seen.append(messages)
            return "Была в норе у обсидиана."

        async def speak(text: str) -> None:
            return None

        async def core() -> str:
            return "Ты Опечатка."

        async def recall(query: str) -> str:
            return "Что я помню по теме:\n- Была в норе, внизу обсидиан."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", recall=recall)
        brain.hear("Опечатка, где ты уже была в квадратном мире?")
        await settle(brain)
        assert "в норе, внизу обсидиан" in seen[0][0]["content"]
        ((_, text),) = brain.take()  # canon memory is no diary: the slow mind checks too
        assert "записи и память" in text

    asyncio.run(scenario())


def test_memory_that_is_empty_or_slow_leaves_it_to_the_slow_mind(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def ask(messages: list[dict[str, str]]) -> str:
            return "Дай вспомню."

        async def speak(text: str) -> None:
            return None

        async def core() -> str:
            return "Ты Опечатка."

        async def recall(query: str) -> str:
            raise TimeoutError

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", recall=recall)
        brain.hear("Опечатка, где ты уже была в квадратном мире?")
        await settle(brain)
        ((_, text),) = brain.take()
        assert "записи и память" in text

    asyncio.run(scenario())


def test_a_promise_to_check_always_comes_with_a_hand_over(tmp_path: Path) -> None:
    async def scenario() -> None:
        brain, said, _ = make(tmp_path, "Дай секунду, сверюсь с заметками.")  # no ДЕЛО
        brain.hear("Опечатка, расскажи про свою поездку")
        await settle(brain)
        assert said == ["Дай секунду, сверюсь с заметками."]
        ((_, text),) = brain.take()
        assert "Не повторяй" in text

    asyncio.run(scenario())


def test_say_drop_bang_speaks_the_exact_words(tmp_path: Path) -> None:
    from ai_agent_core.m3_composition.harness_brain import SayDrop

    async def scenario() -> None:
        brain, said, seen = make(tmp_path, "не должна вызываться")
        folder = tmp_path / "say"
        folder.mkdir()
        (folder / "a.say").write_text("!зам+ок звон+ит", encoding="utf-8")
        SayDrop(brain, folder, every=0).tick()
        await asyncio.sleep(0.05)
        assert said == ["зам+ок звон+ит"] and seen == []  # the model is not asked

    asyncio.run(scenario())


def test_take_sentences_splits_and_stops_at_the_marker() -> None:
    assert take_sentences("Секунду. Сейчас гляну") == (["Секунду."], "Сейчас гляну")
    assert take_sentences("Иду. ДЕЛО: узнай. Точно.") == (["Иду."], "ДЕЛО: узнай. Точно.")
    assert take_sentences("Цена 3.5 рубля") == ([], "Цена 3.5 рубля")  # no split inside a number
    assert take_sentences("Да! Нет? Ладно… Ок") == (["Да!", "Нет?", "Ладно…"], "Ок")


def test_the_first_sentence_is_spoken_while_the_model_still_writes(tmp_path: Path) -> None:
    async def scenario() -> None:
        log: list[str] = []
        gate = asyncio.Event()

        async def stream(messages: list[dict[str, str]]):
            yield "Секунду. Сей"
            log.append("first-piece-sent")
            await gate.wait()  # the model is still writing: the rest has not come
            yield "час гляну в блокнот. ДЕЛО: прочитать цели"

        async def ask(messages: list[dict[str, str]]) -> str:
            return ""

        said: list[str] = []

        async def speak(text: str) -> None:
            log.append("said:" + text)
            said.append(text)

        async def core() -> str:
            return "Ты Опечатка."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", stream=stream)
        brain.hear("Опечатка, привет")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if said:
                break
        assert said == ["Секунду."]  # spoken before the model finished
        gate.set()
        await settle(brain)
        assert said == ["Секунду.", "Сейчас гляну в блокнот."]
        ((_, text),) = brain.take()
        assert "прочитать цели" in text and "Секунду. Сейчас гляну" in text

    asyncio.run(scenario())


def test_a_stream_that_dies_before_any_speech_goes_to_the_slow_mind(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def stream(messages: list[dict[str, str]]):
            raise TimeoutError
            yield ""

        async def ask(messages: list[dict[str, str]]) -> str:
            return ""

        async def speak(text: str) -> None:
            raise AssertionError("nothing to say")

        async def core() -> str:
            return "Ты Опечатка."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", stream=stream)
        brain.hear("Опечатка, привет")
        await settle(brain)
        ((_, text),) = brain.take()
        assert "Быстрый голос не ответил" in text

    asyncio.run(scenario())


def test_the_next_sentence_is_made_while_the_first_is_spoken(tmp_path: Path) -> None:
    async def scenario() -> None:
        order: list[str] = []

        async def prefetch(text: str):
            order.append("made:" + text)
            yield b"\x00\x00"

        async def ask(messages: list[dict[str, str]]) -> str:
            return "Привет. Как дела? Я рада."

        async def speak(text: str, synth=None) -> str:
            order.append("say:" + text)
            await asyncio.sleep(0.05)  # the first one is still playing
            return "Сказано."

        async def core() -> str:
            return "Ты Опечатка."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", prefetch=prefetch)
        brain.hear("Опечатка, привет")
        await settle(brain)
        # all three are made before the first has finished playing: no gaps between them
        assert [o for o in order if o.startswith("made")] == [
            "made:Привет.", "made:Как дела?", "made:Я рада.",
        ]  # fmt: skip
        assert order.index("made:Я рада.") < order.index("say:Как дела?")

    asyncio.run(scenario())


def test_a_failed_sentence_is_tried_once_more(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def ask(messages: list[dict[str, str]]) -> str:
            return "Привет."

        async def speak(text: str) -> str:
            calls.append(text)
            return "Не удалось сказать (SONIOX_TRANSPORT_ERROR)." if len(calls) == 1 else "Сказано."

        async def core() -> str:
            return "Ты Опечатка."

        brain = VoiceBrain(ask, speak, core, tmp_path / "t.log")
        brain.hear("Опечатка, привет")
        await settle(brain)
        assert calls == ["Привет.", "Привет."]

    asyncio.run(scenario())
