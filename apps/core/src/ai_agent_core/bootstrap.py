"""Validated, stopped bootstrap; no listener, job loop, DB or provider at import."""

import json
from importlib.metadata import version

from ai_agent_adapters.configuration import (
    BootstrapSettings,
    ConfigurationError,
    fresh_startup,
    from_environment,
    validate_bindings,
)
from fastapi import FastAPI


def create_app(
    settings: BootstrapSettings | None = None, *, previous_state: str | None = None
) -> FastAPI:
    """Validate BEFORE constructing the shell; authenticated API/supervisor belongs to M1."""
    configuration = from_environment() if settings is None else settings
    validate_bindings(configuration)
    startup = fresh_startup(previous_state)
    app = FastAPI(
        title="AI Agent Platform",
        version=version("ai-agent-platform"),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bootstrap = startup
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
