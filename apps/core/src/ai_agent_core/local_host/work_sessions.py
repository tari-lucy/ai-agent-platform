"""Explicit offline preparation of work conversations through a pinned OpenCode image (R5).

`enable-work-sessions` verifies the operator-built image by its immutable ID, writes the
session capability's manifest and ready binding, and records the runner ceilings as host data;
serve then wires the existing session driver to OpenCode. Nothing is downloaded or built here,
no Permission is written (the operator decides through the structured permission decision,
within the optional ceiling) and the agent stays STOPPED. `check` reports readiness by fixed
codes; serve keeps work conversations unavailable, with the code, when it is not ready.
"""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.agent_workplace.ports import RunnerLimits
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import CapabilityBinding, CapabilityManifest
from pydantic import ValidationError

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, put

from . import rights
from .init import work_scope
from .layout import (
    HostConfig,
    HostError,
    PermissionCeiling,
    WorkSessionsConfig,
    load_config,
    paths,
    read_secret,
    save_config,
)
from .process import exclusive_host

if TYPE_CHECKING:
    from ai_agent_core.m3_composition.service import Runtime

ADAPTER = "opencode"
VERSION = "1.19.3"  # the pinned release of deploy/opencode; the image carries it as a label
LABEL = "ai-agent-platform.opencode.version"
SUMMARY = "Рабочий разговор: ответить оператору, работая с файлами и навыками рабочего места."
INSTRUCTIONS = (
    "Исполняется в изолированной песочнице без сети; модель — через общий gateway и бюджет. "
    "Доступ к файлам, навыкам и тикетам — только через владельцев и текущие права; "
    "ответ становится репликой разговора после подтверждения доставки."
)


def check(settings: WorkSessionsConfig, *, timeout: float = 10.0) -> str | None:
    """None when the Docker engine answers and holds the exact pinned image; else a code."""
    if shutil.which("docker") is None:
        return "DOCKER_NOT_FOUND"
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", settings.image_id],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired, OSError:
        return "DOCKER_UNAVAILABLE"
    if found.returncode != 0:
        try:
            engine = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired, OSError:
            return "DOCKER_UNAVAILABLE"
        return "DOCKER_UNAVAILABLE" if engine.returncode else "OPENCODE_IMAGE_MISSING"
    try:
        [info] = json.loads(found.stdout)
        labels = info["Config"].get("Labels") or {}
        if info["Id"] != settings.image_id or info["Os"] != "linux" or labels.get(LABEL) != VERSION:
            return "OPENCODE_IMAGE_UNEXPECTED"
    except ValueError, KeyError, TypeError:
        return "OPENCODE_IMAGE_UNEXPECTED"
    return None


def template(settings: WorkSessionsConfig) -> RunnerTemplate:
    return RunnerTemplate(
        settings.capability_id,
        settings.image_id,
        PREFIX,
        RunnerLimits(
            settings.cpu_seconds,
            settings.memory_mb,
            settings.pids,
            settings.wall_seconds,
            settings.output_bytes,
            settings.disk_mb * 1024 * 1024,
        ),
        settings.max_input_bytes,
        max_files=settings.max_files,
        model_role_id="activity",
        max_model_calls=settings.max_model_calls,
        max_prompt_chars=settings.max_prompt_chars,
        max_model_tokens=settings.max_model_tokens,
        max_workspace_calls=settings.max_workspace_calls,
        max_helper_calls=settings.helper_max_calls if settings.helper_enabled else 0,
        monitor_interval_seconds=settings.monitor_interval_seconds,
    )


HELPER_SUMMARY = "Запуск Python-скриптов личности в изолированном контейнере без сети."
HELPER_INSTRUCTIONS = (
    "Скрипт и входные файлы подаются владельцем; сеть, установка пакетов и пути host "
    "недоступны. Результаты и диагностика возвращаются файлами через владельца; "
    "запуск требует отдельного разрешения оператора."
)


def helper_template(settings: WorkSessionsConfig) -> RunnerTemplate | None:
    """The networkless script container beside the session; None keeps helpers off.

    The runner template keeps the session's own id (one runner family, one attempt
    capability); ``helper_capability_id`` is the separate permission gate for its use.
    """
    if not settings.helper_enabled:
        return None
    from ai_agent_core.m3_composition.work_session_helpers import PREFIX as HELPER_PREFIX

    return RunnerTemplate(
        settings.capability_id,
        settings.image_id,
        HELPER_PREFIX,
        RunnerLimits(
            settings.helper_cpu_seconds,
            settings.helper_memory_mb,
            settings.helper_pids,
            settings.helper_wall_seconds,
            settings.helper_output_bytes,
            settings.helper_disk_mb * 1024 * 1024,
        ),
        settings.helper_max_input_bytes,
        max_files=settings.helper_max_files,
        max_prompt_chars=1000,  # helpers carry no model call; the field is still bounded
    )


def install(runtime: Runtime, config: HostConfig) -> str | None:
    """Wire the session driver to OpenCode at serve; returns the code that kept it off."""
    settings = config.work_sessions
    if settings is None:
        return None
    if (
        runtime.workplace is None
        or runtime.activity is None
        or config.live is None
        or not config.live.activity.enabled
    ):
        return "WORK_SESSIONS_ACTIVITY_REQUIRED"
    reason = check(settings)
    if reason is not None:
        return reason
    from ai_agent_core.m3_composition.work_session_opencode import OpenCodeSessions

    helper = helper_template(settings)
    OpenCodeSessions(
        runtime,
        template(settings),
        output_area=settings.output_area,
        prewarm_enabled=settings.prewarm_enabled,
        prewarm_idle_seconds=settings.prewarm_idle_seconds,
        helper_template=helper,
        helper_capability=settings.helper_capability_id if helper is not None else None,
    ).install()
    return None


async def enable_work_sessions(
    root: Path, *, image_id: str, areas: list[str], output_area: str,
    ceiling_days: int | None = None, monitor_interval_seconds: float = 1.0,
    prewarm_enabled: bool | None = None, prewarm_idle_seconds: int | None = None,
    helper_enabled: bool | None = None, helper_capability_id: str | None = None, **limits: int,
) -> WorkSessionsConfig:  # fmt: skip
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host)
        read_secret(host.password_hash)  # proves the private prepared host boundary; never printed
        if config.workplace_files is None or not config.workplace_tickets:
            raise HostError("WORK_SESSIONS_FILES_REQUIRED")
        if config.live is None or not config.live.activity.enabled:
            raise HostError("WORK_SESSIONS_ACTIVITY_REQUIRED")
        try:
            settings = WorkSessionsConfig.model_validate(
                dict(
                    image_id=image_id,
                    areas=tuple(areas),
                    output_area=output_area,
                    monitor_interval_seconds=monitor_interval_seconds,
                    prewarm_enabled=(
                        prewarm_enabled
                        if prewarm_enabled is not None
                        else config.work_sessions.prewarm_enabled
                        if config.work_sessions
                        else False
                    ),
                    prewarm_idle_seconds=(
                        prewarm_idle_seconds
                        if prewarm_idle_seconds is not None
                        else config.work_sessions.prewarm_idle_seconds
                        if config.work_sessions
                        else 120
                    ),
                    helper_enabled=(
                        helper_enabled
                        if helper_enabled is not None
                        else config.work_sessions.helper_enabled
                        if config.work_sessions
                        else False
                    ),
                    helper_capability_id=(
                        helper_capability_id
                        if helper_capability_id is not None
                        else config.work_sessions.helper_capability_id
                        if config.work_sessions
                        else "workplace.scripts"
                    ),
                    **limits,
                )
            )
            template(settings)  # the runner's own bounds, before anything is written
            helper_template(settings)  # the script container's bounds, same strictness
        except ValidationError, ValueError:
            raise HostError("WORK_SESSIONS_LIMITS") from None
        previous = config.work_sessions
        if previous is not None and (
            previous.capability_id != settings.capability_id or previous.areas != settings.areas
        ):
            raise HostError("WORK_SESSIONS_AREAS_FIXED")  # the manifest is never rewritten here
        reason = check(settings)
        if reason is not None:
            raise HostError(reason)
        ceilings = dict(config.workplace_permissions or {})
        if ceiling_days is not None:
            ceilings[settings.capability_id] = PermissionCeiling(
                level="automatic", max_days=ceiling_days
            )
            if settings.helper_enabled:
                ceilings[settings.helper_capability_id] = PermissionCeiling(
                    level="automatic", max_days=ceiling_days
                )
        updated = config.model_copy(
            update={"work_sessions": settings, "workplace_permissions": ceilings or None}
        )
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        now = datetime.now(UTC)
        manifest = CapabilityManifest(
            id="manifest-" + settings.capability_id,
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            capability_id=settings.capability_id,
            capability_version=1,
            summary=SUMMARY,
            instructions=(
                INSTRUCTIONS
                + (
                    " Для разборов и вычислений можно сохранять и запускать Python-скрипты "
                    f"(инструмент scripts, возможность {settings.helper_capability_id}): "
                    "исполнение идёт в отдельном контейнере без сети и установки пакетов."
                    if settings.helper_enabled
                    else ""
                )
            ),
            input_schema_id="session.instruction",
            output_schema_id="session.result",
            effect="local_write",
            areas=list(settings.areas),
            network="gateway",
            runner_required=True,
        )
        binding = CapabilityBinding(
            id=ADAPTER + "." + settings.capability_id,
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            binding_id=ADAPTER + "." + settings.capability_id,
            capability_id=settings.capability_id,
            capability_version=1,
            adapter_id=ADAPTER,
            adapter_version=VERSION,
            state="ready",
            verified_at=now,
        )
        seeded: list[tuple[str, CapabilityManifest | CapabilityBinding]] = [
            ("manifest", manifest),
            ("binding", binding),
        ]
        if settings.helper_enabled:
            # The person discovers scripts through this capability's own manifest; a call
            # still needs its explicit permission decision with the covered areas.
            seeded.append(
                (
                    "helper-manifest",
                    CapabilityManifest(
                        id="manifest-" + settings.helper_capability_id,
                        scope=scope,
                        created_at=now,
                        updated_at=now,
                        version=1,
                        capability_id=settings.helper_capability_id,
                        capability_version=1,
                        summary=HELPER_SUMMARY,
                        instructions=HELPER_INSTRUCTIONS,
                        input_schema_id="session.instruction",
                        output_schema_id="session.result",
                        effect="local_write",
                        areas=list(settings.areas),
                        network="none",
                        runner_required=True,
                    ),
                )
            )
            seeded.append(
                (
                    "helper-binding",
                    CapabilityBinding(
                        id=ADAPTER + "." + settings.helper_capability_id,
                        scope=scope,
                        created_at=now,
                        updated_at=now,
                        version=1,
                        binding_id=ADAPTER + "." + settings.helper_capability_id,
                        capability_id=settings.helper_capability_id,
                        capability_version=1,
                        adapter_id=ADAPTER,
                        adapter_version=VERSION,
                        state="ready",
                        verified_at=now,
                    ),
                )
            )
        workspace = PRINCIPALS[Owner.WORKSPACE]
        try:
            for name, row in seeded:
                if await store.repository(type(row), workspace, scope).get(row.id):
                    continue  # written by an earlier or interrupted run; never replaced here
                await put(
                    store,
                    registry,
                    Owner.WORKSPACE,
                    scope,
                    f"work-sessions-{name}",
                    row,
                    None,
                    rights.record_readers(),
                )
        finally:
            await store.close()
        # Records first: a crash before this line leaves work conversations off.
        save_config(host, updated)
        return settings
