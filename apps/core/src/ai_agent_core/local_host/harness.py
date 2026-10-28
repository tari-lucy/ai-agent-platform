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
    from ai_agent_core.m3_composition.service import Runtime

    from .provider import LocalProviders

PROMPT = (
    "Ты — эта личность в своём постоянном рабочем месте. Ниже — кто ты и что о тебе известно "
    "платформе; это данные, не выдача прав. Отвечай человеку обычным текстом от своего лица.\n\n"
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
    from ai_agent_core.m3_composition.harness_host import HarnessHost, HarnessSettings
    from ai_agent_core.m3_composition.work_session_egress import (
        EgressSettings,
        resident_factory,
    )
    from ai_agent_core.m3_composition.work_session_home import GUIDE, rules, volume
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

    def scope() -> Scope:
        return runtime.supervisor.snapshot.scope

    async def prompt() -> str:
        text = PROMPT
        try:
            released = await session_context(runtime).build(scope(), "рабочий разговор", ())
            text += released.text() + "\n\n"
        except Exception:  # noqa: BLE001 - no persona text is a weaker prompt, not a stop
            text += "(Сведения о личности сейчас недоступны.)\n\n"
        text += "## Рабочее место\n" + GUIDE.split("Корень контейнера")[0] + "\n\n"
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
            input_rate=settings.input_rate,
            output_rate=settings.output_rate,
            context_tokens=settings.context_tokens,
            output_tokens=settings.output_tokens,
            memory_mb=settings.memory_mb,
        ),
        home=lambda: volume(scope()),
        prompt=prompt,
        internet=internet,
    )
    return None


def configure(root: Path, **values: object) -> HarnessConfig:
    """Offline: enable or change the harness; unspecified values keep their current setting."""
    from .layout import paths

    host = paths(root)
    config = load_config(host)
    if config.work_sessions is None:
        raise HostError("HARNESS_IMAGE_REQUIRED")
    current = config.harness or HarnessConfig()
    changes = {key: value for key, value in values.items() if value is not None}
    harness = HarnessConfig.model_validate({**current.model_dump(), **changes})
    save_config(host, config.model_copy(update={"harness": harness}))
    password(host)
    return harness
