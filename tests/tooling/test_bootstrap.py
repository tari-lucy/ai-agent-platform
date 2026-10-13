"""Packaging and no-startup smoke tests; no model, broker, DB or provider."""

import importlib
from importlib.metadata import version

from ai_agent_core.bootstrap import create_app
from ai_agent_worker.bootstrap import main as check_worker


def test_installed_packages_and_empty_core() -> None:
    for name in (
        "ai_agent_core",
        "ai_agent_worker",
        "ai_agent_contracts",
        "ai_agent_domain",
        "ai_agent_application",
        "ai_agent_adapters",
    ):
        assert importlib.import_module(name).__file__ is not None
    app = create_app()
    assert app.version == version("ai-agent-platform")
    assert app.routes == []
    assert app.router.on_startup == []


def test_worker_check_does_not_start_jobs(capsys) -> None:
    check_worker()
    assert "no jobs started" in capsys.readouterr().out
