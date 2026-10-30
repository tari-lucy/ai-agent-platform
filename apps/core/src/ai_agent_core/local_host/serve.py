"""Open an initialised host directory STOPPED on loopback. No migration, grant or auto-start."""

import asyncio
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import uvicorn
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.llm import ADAPTER_ID
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.laboratory import LabRunner
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from fastapi import FastAPI
from pydantic import SecretStr

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.app import create_m3_app
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.gm_cycle import PRIVATE_FAMILIES
from ai_agent_core.m3_composition.service import PrivateAPIBudget
from ai_agent_core.m3_composition.state import OPERATOR, PRINCIPALS

from . import rights
from .layout import (
    PERSONA_PENDING,
    HostConfig,
    HostError,
    HostPaths,
    host_scopes,
    load_config,
    paths,
    read_secret,
)
from .maintenance import HostMaintenance
from .panel import mount, validate
from .process import exclusive_host, loopback_socket, open_when_ready
from .provider import LocalProviders, provider_factory

LOOPBACK = "127.0.0.1"


async def _no_scope(_label: str) -> Scope:
    # Test scopes are reserved from the prepared pool; the runner never allocates its own.
    raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)


def build(
    host: HostPaths,
    config: HostConfig,
    *,
    provider: str,
    port: int | None = None,
    panel_dir: Path | None = None,
    env_file: Path | None = None,
    opencode_ui_dir: Path | None = None,
) -> tuple[FastAPI, Callable[[], None]]:
    """Compose the app. Returns it with a closer for the indexes this function opened."""
    prepared = _prepare(
        host, config, provider=provider, port=port, panel_dir=panel_dir, env_file=env_file
    )
    _old_window(opencode_ui_dir)
    return _build(host, config, prepared)


def _old_window(opencode_ui_dir: Path | None) -> None:
    """The old /opencode/ window through the platform is gone (doc33: the harness's own port).

    The option is still accepted so existing launch scripts keep working; nothing is served.
    """
    if opencode_ui_dir is not None:
        print(
            "NOTICE: --opencode-ui-dir is ignored: the old /opencode/ window was removed; "
            "OpenCode opens at the harness port",
            file=sys.stderr,
        )


def _prepare(
    host: HostPaths,
    config: HostConfig,
    *,
    provider: str,
    port: int | None,
    panel_dir: Path | None,
    env_file: Path | None,
) -> tuple[LocalProviders, Path | None, str, int]:
    if (host.root / PERSONA_PENDING).exists() or (host.root / PERSONA_PENDING).is_symlink():
        raise HostError("PERSONA_ADD_INCOMPLETE")
    if (host.root / "RESTORE-PENDING").exists():
        raise HostError("RESTORE_INCOMPLETE")
    if port is not None and not 1024 <= port <= 65535:
        raise HostError("INVALID_OPTION")
    factory = provider_factory(provider, live=config.live, env_file=env_file)
    # Credential/config refusals above happen before opening the database or indexes.
    if not host.database.is_file():
        raise HostError("INIT_INCOMPLETE")
    panel = None if panel_dir is None else validate(panel_dir, host.root)
    number = port or config.port
    secret = read_secret(host.password_hash)
    return factory, panel, secret, number


def _build(
    host: HostPaths,
    config: HostConfig,
    prepared: tuple[LocalProviders, Path | None, str, int],
) -> tuple[FastAPI, Callable[[], None]]:
    factory, panel, secret, number = prepared
    scopes = host_scopes(config)
    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    opened: dict[Path, RecallIndex] = {}

    def lab_index() -> RecallIndex:
        path = host.lab_indexes / (uuid4().hex + ".sqlite")
        index = SQLiteRecallIndex(path)
        opened[path] = index
        return index

    dialogue = SQLiteRecallIndex(host.dialogue_index)
    lab = LabRunner(
        store,
        _no_scope,
        lab_index,
        report_access=(RecordAccess(OPERATOR.id, api_transfer=True),),
    )
    reports = Reports(
        host.reports,
        store,
        registry,
        readers=rights.report_readers(),
        api_transfer_allowed=True,
    )
    settings = BootstrapSettings(
        workspace_id=config.workspace_id,
        persona_id=config.persona_id,
        core_host_port=number,
        operator_origin=f"http://{LOOPBACK}:{number}",
        operator_password_hash=SecretStr(secret),
    )
    from ai_agent_core.access.sessions import AccessLimits

    app = create_m3_app(
        settings,
        store,
        registry,
        # The personal panel on 127.0.0.1: a working day per sign-in, an hour without use.
        access_limits=AccessLimits(session_seconds=12 * 3600, idle_seconds=3600),
        providers=factory,
        gm_auto_start=bool(config.live and config.live.gm.enabled and config.live.gm.auto_start),
        allowed_adapters=frozenset({"mock", ADAPTER_ID} if config.live else {"mock"}),
        private_api_budget=None
        if config.live is None
        else PrivateAPIBudget(
            "conn-live",
            config.live.attempt_limit,
            config.live.max_prompt_chars,
            config.live.attempt_period,
            config.live.memory_processing,
            config.live.memory_daily_calls,
            gm_enabled=config.live.gm.enabled,
            gm_daily_calls=config.live.gm.daily_calls,
            gm_daily_offers=config.live.gm.daily_offers,
            gm_interval_seconds=config.live.gm.interval_minutes * 60.0,
            gm_dialogue_floor=config.live.gm.dialogue_floor,
            activity_enabled=config.live.activity.enabled,
            activity_daily_calls=config.live.activity.daily_calls,
        ),
        scopes=scopes,
        reader_access=rights.record_readers(),
        dialogue_index=dialogue,
        lab=lab,
        reports=reports,
    )

    runtime = app.state.runtime
    runtime.private_social = config.private_social
    if config.workplace_tickets:
        from ai_agent_application.tickets import TicketService

        runtime.tickets = TicketService(
            runtime.store,
            runtime.supervisor.clock.utc,
            readers=rights.ticket_readers(),
            principal=PRINCIPALS[Owner.TICKETS],
        )
    if config.live is not None and config.live.activity.enabled:
        if not config.workplace_tickets:
            raise HostError("TICKETS_REQUIRED")
        from ai_agent_application.agent_activity.model import ActivitySettings

        activity = config.live.activity
        runtime.activity = ActivitySettings(
            cooldown_seconds=activity.cooldown_minutes * 60.0,
            own_goals=activity.own_goals,
            own_goal_daily=activity.own_goal_daily,
            max_open_tasks=activity.max_open_tasks,
        )
    if config.workplace_permissions is not None:
        from ai_agent_core.m3_composition.permission_decisions import Ceiling

        runtime.permission_ceiling = {
            key: Ceiling(value.level, None if value.max_days is None else value.max_days * 86400)
            for key, value in config.workplace_permissions.items()
        }
    if config.live is not None and config.live.gm.enabled:
        # Explicitly enabled offline: the private family and the operator's own line only.
        runtime.families = PRIVATE_FAMILIES
        if config.live.gm.line_direction is not None:
            runtime.gm_line = (config.live.gm.line_direction, config.live.gm.line_boundaries)

    if config.recovery is not None:
        app.state.runtime.maintenance = HostMaintenance(host, config, app.state.runtime, opened)

    if config.workplace_files is not None:
        # Explicitly prepared offline; the release guard needs the host deletion ledger.
        from ai_agent_application.workspace_files import FileLimits

        from ai_agent_core.m3_composition.workplace import build as build_workplace

        assert isinstance(runtime.maintenance, HostMaintenance)  # config requires recovery
        files = config.workplace_files
        runtime.workplace = build_workplace(
            runtime,
            runtime.maintenance.ledger,
            Path(files.blob_dir),
            FileLimits(
                files.max_file_bytes, files.max_scope_bytes, files.max_files, files.max_versions
            ),
        )
        from ai_agent_core.m3_composition.workplace_tools import executors

        # Callable by activity steps only with the agent's current right (checked per call).
        runtime.tool_executors.update(executors(runtime, runtime.workplace))

    harness_on = config.harness is not None and config.harness.enabled
    if config.work_sessions is not None and not harness_on:
        # Explicitly prepared offline; the existing session driver over real OpenCode. Not
        # ready (no Docker/image, no activity) keeps conversations waiting, never runs them.
        # With the doc33 harness on, its settings (image, egress, wakes) serve the harness only.
        from .work_sessions import install

        reason = runtime.session_unavailable = install(runtime, config, host.guide, host.egress_log)
        if reason is not None:
            print(f"NOTICE: work conversations unavailable: {reason}", file=sys.stderr)

    if config.harness is not None:
        # doc33: the persona's own OpenCode server; it replaces the old work loop while on.
        from .harness import install as install_harness

        reason = install_harness(runtime, config, host, factory)
        if reason is not None:
            print(f"NOTICE: harness unavailable: {reason}", file=sys.stderr)
        elif runtime.harness is not None:
            print(
                f"Harness: opencode attach http://127.0.0.1:{config.harness.port} "
                "(after START; password in data/harness/password)",
                file=sys.stderr,
            )

    if config.live is not None and config.operator_controls:
        from .live_budget import LiveBudget

        lock = (
            runtime.maintenance.ledger.lock
            if isinstance(runtime.maintenance, HostMaintenance)
            else asyncio.Lock()
        )
        runtime.budget_controls = LiveBudget(host, runtime, lock)

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def provider_lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            async with previous_lifespan(application):
                yield
        finally:
            # Runtime shutdown settles/cancels jobs first; no idle or in-flight socket survives.
            await factory.aclose()

    app.router.lifespan_context = provider_lifespan

    if panel is not None:
        mount(app, panel)

    def close() -> None:
        for index in opened.values():
            assert isinstance(index, SQLiteRecallIndex)
            index.close()
        dialogue.close()

    return app, close


def permissions_expired(config: HostConfig) -> bool:
    return datetime.fromisoformat(config.permission_expires_at) <= datetime.now(UTC)


def serve(
    root: Path,
    *,
    provider: str,
    port: int | None = None,
    panel_dir: Path | None = None,
    env_file: Path | None = None,
    open_browser: bool = False,
    opencode_ui_dir: Path | None = None,
) -> None:
    host = paths(root)
    if not host.root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    # Read configuration while holding ownership, so an offline cap update cannot race startup.
    with exclusive_host(host.root):
        config = load_config(host)
        number = config.port if port is None else port
        if not 1024 <= number <= 65535:
            raise HostError("INVALID_OPTION")
        if open_browser and panel_dir is None:
            raise HostError("PANEL_REQUIRED")
        prepared = _prepare(
            host, config, provider=provider, port=port, panel_dir=panel_dir, env_file=env_file
        )
        _old_window(opencode_ui_dir)
        with loopback_socket(number) as listener:
            app, close = _build(host, config, prepared)
            stopped = threading.Event()
            browser = None
            try:
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=LOOPBACK,
                        port=number,
                        workers=1,
                        access_log=False,
                        log_level="warning",
                        ws="websockets",
                        ws_per_message_deflate=False,
                        ws_max_size=4096,
                        ws_max_queue=1,
                        backlog=128,
                        timeout_keep_alive=5,
                    )
                )
                if open_browser:
                    browser = threading.Thread(
                        target=open_when_ready,
                        args=(server, number, stopped, "/"),
                        daemon=True,
                    )
                    browser.start()
                print(f"panel/API: http://{LOOPBACK}:{number}/ ; agent starts STOPPED", flush=True)
                server.run(sockets=[listener])
            finally:
                stopped.set()
                if browser is not None:
                    browser.join(timeout=1)
                close()
