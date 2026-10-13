"""Validated stopped bootstrap, with one control owner and no import-time IO."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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
from ai_agent_contracts.common import Scope
from fastapi import FastAPI


def create_app(
    settings: BootstrapSettings | None = None, *, previous_state: str | None = None
) -> FastAPI:
    """Construct a fresh stopped supervisor; M1-05 supplies authenticated handlers."""
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watchdog = asyncio.create_task(supervisor.watchdog())
        try:
            yield
        finally:
            supervisor.shutdown()
            watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog

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
    # Credentials are not attached to API state or returned via a route.
    app.state.bootstrap_manifest = startup.safe_manifest(configuration)
    return app


def main() -> None:
    """Safe configuration check; no web listener or active agent."""
    try:
        app = create_app()
    except ConfigurationError as error:
        print(str(error))
        raise SystemExit(78) from None
    print(json.dumps(app.state.bootstrap_manifest, sort_keys=True))
