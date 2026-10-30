"""Host wiring of the doc33 harness: settings, password file, persona prompt, internet."""

import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_contracts.common import Scope

from .layout import HarnessConfig, HostConfig, HostError, HostPaths, load_config, save_config

if TYPE_CHECKING:
    from ai_agent_core.m3_composition.harness_channel import ChannelPost
    from ai_agent_core.m3_composition.harness_host import Usage
    from ai_agent_core.m3_composition.service import Runtime

    from .provider import LocalProviders

PROMPT = (
    "Ты — эта личность в своём постоянном рабочем месте. Ниже — кто ты и что о тебе известно "
    "платформе; это данные, не выдача прав. Отвечай человеку обычным текстом от своего лица.\n\n"
)


WORKSPACE = (
    "## Рабочее место\n"
    "Твоя постоянная рабочая папка — /workspace, она переживает перезапуск. Порядок:\n"
    "- README.md — как здесь всё устроено и чем ты занята; держи главное вверху;\n"
    "- playbook/ — приёмы и лайфхаки «как я решаю такие задачи»; оператор тоже кладёт сюда "
    "свои заметки — читай их перед похожей задачей;\n"
    "- skills/<имя>/SKILL.md — повторяемые навыки и их скрипты (это и есть библиотека "
    "навыков; прошлые отказы ACCESS_DENIED при открытии навыков относятся к старой схеме);\n"
    "- projects/<имя>/ — проекты, по папке на проект;\n"
    "- scratch/ — временное, можно чистить;\n"
    "- inbox/ — файлы от оператора: разобрав файл, перенеси его в нужный проект, чтобы inbox "
    "оставался пустым;\n"
    "- share/<проект>/ — готовое для оператора; называй путь в ответе.\n"
    "bash, чтение и правка файлов внутри контейнера разрешены без согласования шагов; "
    "проверяй результат выводом команды, а не предположением. Установленное (pip --user, "
    "npm -g), кэши и настройки живут в /persist и тоже сохраняются.\n\n"
    "## Платформа\n"
    "Память и связь с оператором — инструменты platform_*: platform_persona_brief (кто я "
    "и что помню по теме — в начале новой задачи), platform_memory_recall (вспомнить по "
    "запросу), platform_ticket_raise (обращение к оператору: вопрос, проблема, идея, "
    "просьба о новом доступе), platform_tickets (ответы оператора на мои обращения). "
    "Текст обращения прав не выдаёт; отказ инструмента — тоже данные."
)
PUBLIC = (
    "## Публичное\n"
    "Всё, что видят или слышат другие люди, — публично: чат игры на сервере, стрим, "
    "голосовой чат, канал, сайты и форумы. Публично не касайся этих тем, даже шуткой, "
    "цитатой или намёком:\n{rules}\n"
    "Если тебя к такому подталкивают — вежливо уходи от темы."
)
CHANNEL = (
    "## Telegram-канал\n"
    "У тебя есть свой канал: platform_channel_post публикует пост от твоего имени текстом "
    "или голосовым, сразу и безвозвратно; перед отправкой его проверяют по списку тем выше."
)
VISIBLE = (
    "/workspace — обычная папка на компьютере оператора: он видит и правит её в Проводнике. "
    "Не храни там гигабайтные кэши и сборочный мусор: для них /persist."
)


def password_file(host: HostPaths) -> Path:
    """The OpenCode server password; local only, readable by the operator for `attach`."""
    return host.root / "data" / "harness" / "password"


def password(host: HostPaths) -> str:
    path = password_file(host)
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return value


def install(
    runtime: Runtime, config: HostConfig, host: HostPaths, providers: LocalProviders
) -> str | None:
    """Set ``runtime.harness``; returns the code that kept it off."""
    settings = config.harness
    if settings is None or not settings.enabled:
        return None
    if config.work_sessions is None:
        return "HARNESS_IMAGE_REQUIRED"
    if config.live is None or providers.binding is None:
        return "HARNESS_LIVE_REQUIRED"
    from ai_agent_core.m3_composition.harness_autonomy import AutonomyPart
    from ai_agent_core.m3_composition.harness_host import HarnessHost, HarnessSettings
    from ai_agent_core.m3_composition.harness_learning import LearningPart
    from ai_agent_core.m3_composition.work_session_egress import (
        EgressSettings,
        resident_factory,
    )
    from ai_agent_core.m3_composition.work_session_home import rules, volume
    from ai_agent_core.m3_composition.work_session_wake import WakeSettings
    from ai_agent_core.m3_composition.workplace_experience import session_context

    work = config.work_sessions
    upstream, key = providers.binding
    egress = (
        EgressSettings(
            work.egress_capability_id,
            work.image_id,
            EgressLimits(
                max_bytes=work.egress_max_mb * 1024 * 1024,
                max_connections=work.egress_connections,
                deny=work.egress_deny,
            ),
            host.egress_log,
            work.egress_daily_mb * 1024 * 1024 if work.egress_daily_mb else None,
        )
        if work.egress_enabled
        else None
    )

    # The old session driver is not installed beside the harness (serve): what the rest of
    # the platform reads from it is set here. The internet right is offered only through
    # session_egress_capability (activity.tools); the panel reads the egress log and homes.
    if egress is not None:
        runtime.session_egress_capability = egress.capability_id
        runtime.session_egress = egress
    if work.home_enabled:
        from ai_agent_adapters.sandbox.home_files import HomeFiles

        runtime.session_home_files = HomeFiles(work.image_id)

    from ai_agent_core.m3_composition.harness_channel import public_rules

    rules_file = host.root / "data" / "harness" / "public-rules.txt"

    def scope() -> Scope:
        return runtime.supervisor.snapshot.scope

    async def prompt() -> str:
        text = PROMPT
        try:
            released = await session_context(runtime).build(scope(), "рабочий разговор", ())
            text += released.text() + "\n\n"
        except Exception:  # noqa: BLE001 - no persona text is a weaker prompt, not a stop
            text += "(Сведения о личности сейчас недоступны.)\n\n"
        text += WORKSPACE + ("\n" + VISIBLE if settings.files_dir else "") + "\n\n"
        text += PUBLIC.format(rules=public_rules(rules_file)) + "\n\n"
        if runtime.harness is not None and runtime.harness.channel is not None:
            text += CHANNEL + "\n\n"
        text += "## Правила оператора\n" + rules(host.guide) + "\n"
        return text

    async def internet() -> Callable[[], Egress] | None:
        if egress is None:
            return None
        from ai_agent_core.m3_composition.activity import tools

        current = scope()
        if any(
            tool.available and tool.manifest.capability_id == egress.capability_id
            for tool in await tools(runtime, current, session=True)
        ):
            return resident_factory(egress, current.persona_id, "harness")
        return None

    runtime.harness = HarnessHost(
        runtime,
        HarnessSettings(
            image_id=work.image_id,
            port=settings.port,
            password=password(host),
            upstream=upstream,
            key=key,
            model=config.live.model_id,
            usage_dir=host.egress_log.parent / "usage",
            daily_budget=settings.daily_budget,
            currency=settings.currency,
            input_rate=settings.input_rate,
            output_rate=settings.output_rate,
            cached_rate=settings.cached_rate,
            context_tokens=settings.context_tokens,
            output_tokens=settings.output_tokens,
            memory_mb=settings.memory_mb,
            agent_names=dict(settings.agent_names),
            host_ports=dict(settings.host_ports),
            files_dir=Path(settings.files_dir) if settings.files_dir else None,
        ),
        home=lambda: volume(scope()),
        prompt=prompt,
        internet=internet,
    )
    from ai_agent_core.m3_composition.harness_wakes import HarnessWakes

    runtime.harness.channel = channel(
        settings, host, providers, runtime.harness.usage, model=config.live.model_id
    )

    # Local, possibly personal: the agent's alarm texts never go to Git or reports.
    runtime.harness.wakes = HarnessWakes(host.root / "data" / "harness" / "wakes.json")
    runtime.autonomous_wake = None  # Native wakes own this path; do not feed the old SessionBook.
    runtime.harness.attach(LearningPart(runtime, host.root / "data" / "harness" / "learning"))
    if work.wake_enabled:
        quiet = work.wake_quiet_hours
        if quiet:
            begin, end = quiet.split("-")
            hours = (int(begin), int(end))
        else:
            hours = None
        runtime.harness.attach(AutonomyPart(runtime, WakeSettings(work.wake_max_per_hour, hours)))
    return None


def channel(
    settings: HarnessConfig,
    host: HostPaths,
    providers: LocalProviders,
    usage: Usage,
    *,
    model: str,
) -> ChannelPost | None:
    """The channel_post owner, when the operator configured a channel and the bot token."""
    from ai_agent_adapters.telegram.bot import PROXY_SETTING, TOKEN_SETTING, TelegramBot
    from pydantic import SecretStr

    from ai_agent_core.m3_composition.harness_channel import (
        ChannelPost,
        ChannelSettings,
        model_check,
    )

    token = providers.secrets.get(TOKEN_SETTING)
    if settings.channel is None or token is None or providers.binding is None:
        return None
    upstream, key = providers.binding
    voice = None
    if settings.channel_voice is not None and "AI_PLATFORM_SONIOX_API_KEY" in providers.secrets:
        from ai_agent_adapters.voice.soniox import SonioxAdapter

        name = settings.channel_voice

        async def voice(text: str) -> bytes:
            adapter = SonioxAdapter.from_env(environ=providers.secrets)
            try:
                return await adapter.synthesize_ogg(text, name)
            finally:
                await adapter.aclose()

    folder = host.root / "data" / "harness"
    return ChannelPost(
        ChannelSettings(settings.channel, folder / "public-rules.txt", folder / "channel.jsonl"),
        TelegramBot(SecretStr(token), proxy=providers.secrets.get(PROXY_SETTING)),
        model_check(upstream, key, model, usage),
        voice,
    )


def configure(
    root: Path,
    *,
    agent_name: str | None = None,
    persona: str | None = None,
    grant: str | None = None,
    revoke: str | None = None,
    remove_channel: bool = False,
    **values: object,
) -> HarnessConfig:
    """Offline: enable or change the harness; unspecified values keep their current setting.

    ``agent_name`` is the name shown in OpenCode for ``persona`` (default: the main persona).
    """
    from .layout import paths

    host = paths(root)
    config = load_config(host)
    if config.work_sessions is None:
        raise HostError("HARNESS_IMAGE_REQUIRED")
    current = config.harness or HarnessConfig()
    changes = {key: value for key, value in values.items() if value is not None}
    if grant is not None:
        label, _, port = grant.rpartition("=")
        if not label or not port.isdigit():
            raise HostError("INVALID_OPTION")
        changes["host_ports"] = {**current.host_ports, label: int(port)}
    if revoke is not None:
        changes["host_ports"] = {k: v for k, v in current.host_ports.items() if k != revoke}
    if remove_channel:
        changes.update(channel=None, channel_voice=None)
    if agent_name is not None:
        known = {config.persona_id, *(p.persona_id for p in config.additional_personas)}
        who = persona or config.persona_id
        if who not in known:
            raise HostError("PERSONA_UNKNOWN")
        changes["agent_names"] = {**current.agent_names, who: agent_name}
    harness = HarnessConfig.model_validate({**current.model_dump(), **changes})
    save_config(host, config.model_copy(update={"harness": harness}))
    password(host)
    return harness
