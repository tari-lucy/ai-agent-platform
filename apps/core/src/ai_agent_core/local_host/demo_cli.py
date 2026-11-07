"""Offline `demo`: play a script through the avatar and the voice, for a video or a rehearsal.

A script is a text file; each line is one thing to do:

    # a comment                      (ignored)
    [радость] Привет! Меня зовут Опечатка.      the face changes, then she says the words
    [смущение]                                   only the face changes
    Просто фраза без эмоции.                     only the words
    [пауза 2]                                    wait two seconds

Emotions may be written in Russian or English (радость, грусть, злость, удивление, раздумье,
смущение, сонливость, нейтрально; happy, sad, angry, surprised, thinking, embarrassed, sleepy,
neutral). The persona must be stopped (the demo drives the avatar and the voice itself); no model
is called, so nothing waits and nothing varies: the same script gives the same take. Before the
first line there is a countdown, so that you can press record in OBS.

    demo --root HOST --env-file ENV --script demo.txt [--countdown 5] [--pause 0.8]
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_agent_application.avatar import Emotion

from .harness import AvatarPart
from .harness import avatar as build_avatar
from .harness import speaker as build_speaker
from .layout import HostError, load_config, paths

NAMES = {
    "радость": Emotion.HAPPY,
    "радостный": Emotion.HAPPY,
    "грусть": Emotion.SAD,
    "злость": Emotion.ANGRY,
    "удивление": Emotion.SURPRISED,
    "раздумье": Emotion.THINKING,
    "задумчивость": Emotion.THINKING,
    "смущение": Emotion.EMBARRASSED,
    "сонливость": Emotion.SLEEPY,
    "нейтрально": Emotion.NEUTRAL,
    "спокойно": Emotion.NEUTRAL,
    **{e.value: e for e in Emotion},
}
TAG = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
PAUSE = re.compile(r"^пауза\s+(\d+(?:[.,]\d+)?)$", re.IGNORECASE)
MIN_CHANGE = 1.6  # seconds between two face changes (the avatar's own mixer asks for 1.5)
MAX_LINES = 200


class ScriptError(Exception):
    """A line of the script that cannot be understood: the message names the line."""


@dataclass(frozen=True)
class Step:
    emotion: Emotion | None = None
    text: str = ""
    pause: float = 0.0


def parse_script(text: str) -> list[Step]:
    steps: list[Step] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        emotion: Emotion | None = None
        words = line
        match = TAG.match(line)
        if match:
            tag, words = match.group(1).strip().lower(), match.group(2).strip()
            waiting = PAUSE.match(tag)
            if waiting:
                steps.append(Step(pause=float(waiting.group(1).replace(",", "."))))
                if words:
                    steps.append(Step(text=words))
                continue
            if tag not in NAMES:
                raise ScriptError(f"строка {number}: неизвестная эмоция «{tag}»")
            emotion = NAMES[tag]
        if len(words) > 600:
            raise ScriptError(f"строка {number}: фраза длиннее 600 знаков")
        steps.append(Step(emotion=emotion, text=words))
        if len(steps) > MAX_LINES:
            raise ScriptError(f"сценарий длиннее {MAX_LINES} шагов")
    return steps


async def play(
    steps: list[Step],
    avatar: Any,
    speaker: Any,
    *,
    countdown: int,
    pause: float,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Run the steps; True when every spoken line was said."""
    lines = [step.text for step in steps if step.text]
    out(f"Готовлю озвучку ({len(lines)} фраз), это займёт полминуты…")
    await speaker.prepare_acks(lines, limit=600)  # no synthesis wait between the lines later
    for left in range(countdown, 0, -1):
        out(f"Старт через {left}…")
        await sleep(1.0)
    out("Поехали.")
    last_change = -1e9
    for step in steps:
        if step.pause:
            out(f"  пауза {step.pause:g} с")
            await sleep(step.pause)
            continue
        if step.emotion is not None and avatar is not None:
            wait = MIN_CHANGE - (clock() - last_change)
            if wait > 0:
                await sleep(wait)
            result = await avatar.express(step.emotion)
            last_change = clock()
            if not result.applied and "нет выражения" in result.text:
                out(f"  ({step.emotion.value}: в этой модели нет выражения, лицо не меняется)")
        if step.text:
            out(f"  {step.text}")
            said = await speaker.speak(step.text, lambda: None, speaker.prepared(step.text))
            if said != "Сказано.":
                out(f"Не вышло: {said}")
                return False
            await sleep(pause)
    return True


async def run_demo(
    steps: list[Step], root: Path, env_file: Path, *, countdown: int, pause: float
) -> bool:
    from types import SimpleNamespace

    from .provider import provider_factory

    host = paths(root)
    config = load_config(host)
    if config.harness is None or config.live is None:
        raise HostError("HARNESS_NOT_CONFIGURED")
    settings = config.harness
    providers = provider_factory("live", live=config.live, env_file=env_file)
    usage = SimpleNamespace(remaining=lambda: None, record_amount=lambda *a, **k: None)
    avatar = build_avatar(settings, host)
    voice = build_speaker(settings, host, providers, usage, avatar)  # type: ignore[arg-type]
    if voice is None:
        print("Голос не настроен (--speech-voice, --speech-port) или нет ключа Soniox в .env.")
        return False
    keeper = None
    if avatar is not None:
        if not await avatar.ready():
            print(
                "VTube Studio не отвечает: запущен ли он, поднят ли туннель, включён ли Start API?"
            )
            return False
        keeper = AvatarPart(avatar)  # the resting face: eyes open, as while she runs
        keeper.tick()
        await asyncio.sleep(1.0)
    try:
        return await play(steps, avatar, voice, countdown=countdown, pause=pause)
    finally:
        if keeper is not None:
            await asyncio.sleep(2.0)  # the last frame stays in the video for a moment
            await keeper.close()
        if avatar is not None:
            await avatar.close()


def run(root: Path, env_file: Path, script: Path, countdown: int, pause: float) -> int:
    try:
        steps = parse_script(script.read_text(encoding="utf-8"))
    except OSError:
        print(f"Не могу прочитать сценарий: {script}")
        return 2
    except ScriptError as error:
        print(f"Сценарий не разобран, {error}.")
        return 2
    if not any(step.text for step in steps):
        print("В сценарии нет ни одной фразы.")
        return 2
    ok = asyncio.run(run_demo(steps, root, env_file, countdown=countdown, pause=pause))
    return 0 if ok else 1
