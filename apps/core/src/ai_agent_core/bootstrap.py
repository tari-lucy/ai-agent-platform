"""Validated stopped bootstrap, with one control owner and no import-time IO."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from importlib.metadata import version

from ai_agent_adapters.configuration import (
    BootstrapSettings,
    ConfigurationError,
    fresh_startup,
    from_environment,
    validate_bindings,
)
from ai_agent_adapters.control.journal import UnavailableJournal, UnavailableReadiness
from ai_agent_application.control import Supervisor
from ai_agent_application.scheduling import ResourceScheduler, SchedulerLimits
from ai_agent_application.scheduling.model import DEFAULT_POOLS
from ai_agent_application.telemetry import HealthState, Telemetry
from ai_agent_contracts.common import Scope
from fastapi import FastAPI

from .access import AccessPorts, install_access


def create_app(
    settings: BootstrapSettings | None = None,
    *,
    previous_state: str | None = None,
    access_ports: AccessPorts | None = None,
    access_factory: Callable[[Supervisor], AccessPorts] | None = None,
) -> FastAPI:
    """Construct a stopped supervisor with fail-closed authenticated access."""
    configuration = from_environment() if settings is None else settings
    validate_bindings(configuration)
    startup = fresh_startup(previous_state)
    supervisor = Supervisor(
        Scope(
            workspace_id=configuration.workspace_id,
            persona_id=configuration.persona_id,
            memory_space_id=configuration.memory_scope + "-bootstrap",
            memory_mode=configuration.memory_scope,
        ),
        "primary",
        UnavailableJournal(),
        UnavailableReadiness(),
    )

    telemetry = Telemetry(clock=supervisor.clock, critical_failure=supervisor.critical_failure)
    telemetry.register("control")
    telemetry.update_health("control", HealthState.READY)
    pools = tuple(
        replace(pool, capacity=configuration.worker_memory_mib) if pool.id == "ram" else pool
        for pool in DEFAULT_POOLS
    )
    scheduler = ResourceScheduler(
        supervisor,
        telemetry,
        pools=pools,
        limits=SchedulerLimits(pending=configuration.pending_jobs),
    )

    async def operational_watchdog() -> None:
        while True:
            scheduler.poll()
            telemetry.update_health("control", HealthState.READY)
            await asyncio.sleep(0.05)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watchdog = asyncio.create_task(supervisor.watchdog())
        operations = asyncio.create_task(operational_watchdog())
        try:
            yield
        finally:
            access.sessions.shutdown()
            supervisor.shutdown()
            scheduler.poll()
            watchdog.cancel()
            operations.cancel()
            for task in (watchdog, operations):
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="AI Agent Platform",
        version=version("ai-agent-platform"),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.bootstrap = startup
    app.state.supervisor = supervisor
    app.state.telemetry = telemetry
    app.state.scheduler = scheduler
    # Credentials are not attached to API state or returned via a route.
    app.state.bootstrap_manifest = startup.safe_manifest(configuration)
    if access_factory is not None:
        if access_ports is not None:
            raise ConfigurationError("access", "single_composition_required")
        access_ports = access_factory(supervisor)
    access = install_access(app, supervisor, configuration, access_ports)
    return app


def main() -> None:
    """Safe configuration check; no web listener or active agent."""
    try:
        app = create_app()
    except ConfigurationError as error:
        print(str(error))
        raise SystemExit(78) from None
    print(json.dumps(app.state.bootstrap_manifest, sort_keys=True))
