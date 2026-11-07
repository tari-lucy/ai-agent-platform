"""What the slow mind is doing, as a true short line; the fast voice fills pauses with it."""

import asyncio
from pathlib import Path

from ai_agent_core.m3_composition.harness_activity import Activity, phrase
from ai_agent_core.m3_composition.harness_brain import VoiceBrain


def part(tool: str, status: str, call: str = "c1", **state: object) -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "type": "tool", "tool": tool, "callID": call,
                "state": {"status": status, **state},
            }
        },
    }  # fmt: skip


def test_phrase_names_the_kind_never_the_content() -> None:
    assert phrase("bash", {"command": "cat secret.txt"}) == "выполняет команду"
    assert phrase("bash", {"command": "source /workspace/skills/minecraft/env.sh"}) == (
        "работает в игре"
    )
    assert phrase("platform_imagine", {}) == "рисует картинку"
    assert phrase("webfetch", {}) == "ищет в интернете"


def test_activity_follows_steps_and_forgets_on_idle() -> None:
    now = [10.0]
    idle: list[int] = []
    seen: list[tuple[str, str]] = []
    act = Activity("http://x", "p", on_tool=lambda t, s: seen.append((t, s)),
                   on_idle=lambda: idle.append(1), clock=lambda: now[0])  # fmt: skip
    act.feed(part("grep", "running"))
    now[0] = 25.0
    assert act.describe().startswith("сейчас читает файлы (уже 15 с)")
    act.feed(part("grep", "completed"))
    act.feed({"payload": part("platform_speak", "completed", call="s")})  # wrapped event
    assert "недавно: читает файлы" in act.describe()
    assert "secret" not in act.describe() and ("platform_speak", "completed") in seen
    act.feed(part("bash", "running", call="c2"))
    act.feed({"type": "session.idle", "properties": {}})
    assert idle == [1] and "пауза" in act.describe()
    act.feed("garbage")  # never raises


def test_voice_fills_a_long_wait_in_a_live_talk_and_stops_when_she_answers(tmp_path: Path) -> None:
    asyncio.run(_wait(tmp_path))


async def _wait(tmp_path: Path) -> None:
    now = [0.0]
    said: list[str] = []
    seen: list[list[dict[str, str]]] = []
    faces: list[str] = []

    async def ask(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return "Секунду.\nДЕЛО: узнай" if len(seen) == 1 else "Хм, по-моему, их немного."

    async def speak(text: str) -> None:
        said.append(text)

    async def core() -> str:
        return "Ты Опечатка."

    async def express(name: str) -> None:
        faces.append(name)

    brain = VoiceBrain(ask, speak, core, tmp_path / "t.log", express=express,
                       activity=lambda: "сейчас ищет в интернете (уже 9 с)",
                       clock=lambda: now[0])  # fmt: skip
    brain.hear("Опечатка, сколько тут шахт?")
    await asyncio.gather(*list(brain._tasks))
    await asyncio.sleep(0)
    assert faces == ["thinking"] and brain.waiting is not None
    now[0] = 5.0
    brain.tick()
    assert len(said) == 1  # too early: a pause is not yet a silence
    now[0] = 14.0
    brain.tick()
    await asyncio.gather(*list(brain._tasks))
    assert said[-1] == "Хм, по-моему, их немного."
    assert "ищет в интернете" in seen[1][0]["content"] and "ждёшь" in seen[1][1]["content"]
    now[0] = 20.0
    brain.tick()  # a remark was just made: not again for FILLER_GAP
    assert len(said) == 2
    brain.slow_event("platform_voice_brief", "completed")
    now[0] = 60.0
    brain.tick()
    assert len(said) == 2 and brain.waiting is None
