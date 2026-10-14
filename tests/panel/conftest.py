"""Isolated real M1 core plus built panel; no env files, DB, workers or paid calls."""

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from ai_agent_adapters.configuration import BootstrapSettings
from playwright.sync_api import sync_playwright
from pydantic import SecretStr
from starlette.staticfiles import StaticFiles

from ai_agent_core.access import password_hash
from ai_agent_core.bootstrap import create_app

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "synthetic-panel-browser-password"


@pytest.fixture(scope="session")
def live_panel():
    distribution = ROOT / "apps/panel/dist"
    if not (distribution / "index.html").is_file():
        pytest.fail("Build apps/panel before browser acceptance.")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    settings = BootstrapSettings(
        workspace_id="synthetic",
        persona_id="alpha",
        memory_scope="test",
        core_host_port=port,
        operator_origin=origin,
        operator_password_hash=password_hash(SecretStr(PASSWORD)),
    )

    def factory():
        app = create_app(settings)
        app.mount("/", StaticFiles(directory=distribution, html=True), name="panel")
        return app

    server = uvicorn.Server(
        uvicorn.Config(
            factory,
            factory=True,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="warning",
            ws="websockets",
            ws_max_size=4096,
            ws_max_queue=1,
            ws_per_message_deflate=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Isolated core did not start."
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(8)
        listener.close()
    assert not thread.is_alive()


@pytest.fixture(scope="session")
def browser():
    import os

    with sync_playwright() as playwright:
        # Already installed browser is optional for local verification, no download here.
        channel = os.environ.get("AI_PANEL_BROWSER_CHANNEL")
        executable = os.environ.get("AI_PANEL_BROWSER_EXECUTABLE")
        instance = playwright.chromium.launch(
            headless=True,
            **({"channel": channel} if channel else {}),
            **({"executable_path": executable} if executable else {}),
        )
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    tab = context.new_page()
    tab.set_default_timeout(8000)
    errors = []
    tab.on("pageerror", lambda error: errors.append(error.message))
    yield tab
    context.close()
    assert not errors, "Browser JavaScript error"


@pytest.fixture
def panel_password():
    return PASSWORD
