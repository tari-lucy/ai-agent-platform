"""Import/build check without a web listener or an active agent."""

from importlib.metadata import version

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Construct an empty shell; authenticated API belongs to M1."""
    return FastAPI(
        title="AI Agent Platform",
        version=version("ai-agent-platform"),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )


def main() -> None:
    """Verify installed core imports without starting services."""
    create_app()
    print("Core bootstrap OK; no runtime started.")
