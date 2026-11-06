"""Offline `live-test`: try the live connections yourself, without starting the persona.

It uses the host's own settings (what `configure-harness` stored) and the explicit .env binding,
so it checks exactly what a real start would use: the tunnel, the programs on the computer, the
profile, the voice. No model is called and the persona is not running; only speech costs a few
cents of Soniox (not written to the day's ledger). The platform may be stopped or running, but
two things must not talk to VTube Studio at once: stop the persona first.

    live-test avatar             show every emotion of the profile, one by one, then neutral
    live-test speak --text "…"   say it aloud (and move the mouth)
    live-test obs [--scene X]    print the OBS state, optionally switch to scene X
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ai_agent_application.avatar import Emotion

from .harness import AvatarPart
from .harness import avatar as build_avatar
from .harness import speaker as build_speaker
from .harness import stage as build_stage
from .layout import HarnessConfig, HostError, HostPaths, load_config, paths

PAUSE = 1.8  # a little more than the mixer's own pacing, so every change is really shown


async def check(
    what: str,
    settings: HarnessConfig,
    host: HostPaths,
    providers: Any,
    *,
    text: str = "",
    scene: str = "",
    pause: float = PAUSE,
    settle: float = 3.0,
    out: Any = print,
) -> bool:
    """Run one check; every line goes to ``out``. True when it worked."""
    if what == "avatar":
        avatar = build_avatar(settings, host)
        if avatar is None:
            out("Аватар не настроен или профиль не читается (configure-harness --avatar-…).")
            return False
        if not await avatar.ready():
            out("VTube Studio не отвечает: запущен ли он, поднят ли туннель, включён ли Start API?")
            return False
        out(f"На связи. Несовпадения профиля с моделью: {avatar.problems or 'нет'}")
        keeper = AvatarPart(avatar)  # the resting face, as while she runs (eyes open)
        keeper.tick()
        try:
            out("Лицо в покое (3 секунды): глаза должны быть открыты.")
            await asyncio.sleep(settle)
            for emotion in (e for e in Emotion if e is not Emotion.NEUTRAL):
                result = await avatar.express(emotion)
                out(f"{emotion.value}: {result.text}")
                await asyncio.sleep(pause)
            await avatar.neutral()
            out("Лицо возвращено в нейтральное (ещё 3 секунды в покое).")
            await asyncio.sleep(settle)
        finally:
            await keeper.close()
            await avatar.close()
        return True
    if what == "speak":
        usage = SimpleNamespace(remaining=lambda: None, record_amount=lambda *a, **k: None)
        avatar = build_avatar(settings, host)
        speaker = build_speaker(settings, host, providers, usage, avatar)  # type: ignore[arg-type]
        if speaker is None:
            out("Голос не настроен (--speech-voice, --speech-port) или нет ключа Soniox в .env.")
            return False
        said = await speaker.speak(text or "Проверка голоса. Меня слышно?", lambda: None)
        out(said)
        if avatar is not None:
            await avatar.close()
        return said == "Сказано."
    if what == "obs":
        stage = build_stage(settings, providers)
        if stage is None:
            out("OBS не настроен (configure-harness --obs-port …).")
            return False
        out(await stage.status())
        if scene:
            out(await stage.scene(scene))
        await stage.close()
        return True
    raise HostError("INVALID_OPTION")


def run(root: Path, env_file: Path, what: str, text: str, scene: str) -> int:
    from .provider import provider_factory

    host = paths(root)
    config = load_config(host)
    if config.harness is None or config.live is None:
        raise HostError("HARNESS_NOT_CONFIGURED")
    providers = provider_factory("live", live=config.live, env_file=env_file)
    ok = asyncio.run(check(what, config.harness, host, providers, text=text, scene=scene))
    return 0 if ok else 1
