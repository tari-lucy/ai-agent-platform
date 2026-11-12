"""The fast voice brain: it answers by voice, hands actions to the slow mind, never twice."""

import asyncio
from pathlib import Path

from ai_agent_core.m3_composition.harness_brain import VoiceBrain, parse
from ai_agent_core.m3_composition.harness_listen import ListenSource


def test_parse_splits_speech_and_task() -> None:
    assert parse("Сейчас гляну.\nДЕЛО: найди алмазы") == ("Сейчас гляну.", "найди алмазы")
    assert parse("**Привет**, как дела?") == ("Привет, как дела?", "")
    assert parse("-") == ("", "")


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
