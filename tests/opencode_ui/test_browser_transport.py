"""Real Windows loopback serve + Chromium, a synthetic build, no model/API calls."""

import json
import os

import pytest
from ai_agent_contracts.common import Scope
from host_support import Server, free_port, init, scope_query
from playwright.sync_api import sync_playwright

from ai_agent_core.opencode_ui.assets import prepare
from ai_agent_core.opencode_ui.protocol import PREFIX, directory


@pytest.mark.skipif(os.environ.get("AW_OPENCODE_BROWSER") != "1", reason="opt-in browser transport")
def test_real_serve_browser_assets_sse_reload_and_direct_stop(tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    config = json.loads((root / "host.json").read_text())
    selected = Scope(
        workspace_id=config["workspace_id"],
        persona_id=config["persona_id"],
        memory_space_id=config["work_space_id"],
        memory_mode="work",
    )
    build = tmp_path / "frontend"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text(
        '<html><script type="module" src="/opencode/assets/transport.js"></script>'
        "<body>Transport fixture; this is not the native OpenCode UI</body></html>"
    )
    (build / "assets/transport.js").write_text(
        "const stream=new EventSource('" + PREFIX + "/agents/" + directory(selected) + "/events');"
        "stream.addEventListener('connected',()=>document.documentElement.dataset.connected='yes');"
        "stream.addEventListener('refetch',()=>document.documentElement.dataset.refetch='yes');"
    )
    prepare(build)
    with Server(root, free_port(), tmp_path / "server.log", opencode_ui_dir=build) as server:
        with sync_playwright() as p:
            channel = os.environ.get("AI_PANEL_BROWSER_CHANNEL")
            executable = os.environ.get("AI_PANEL_BROWSER_EXECUTABLE")
            browser = p.chromium.launch(
                headless=True,
                **({"channel": channel} if channel else {}),
                **({"executable_path": executable} if executable else {}),
            )
            try:
                context = browser.new_context()
                api = context.request
                login = api.post(
                    server.origin + "/api/v1/auth/login",
                    headers={"Origin": server.origin},
                    data={"password": (root / "secrets/operator-password.txt").read_text().strip()},
                )
                assert login.status == 200
                headers = {"Origin": server.origin, "X-CSRF-Token": login.json()["csrf_token"]}
                page = context.new_page()
                page.goto(server.origin + "/opencode/")
                page.locator("html[data-connected='yes']").wait_for(state="attached")
                page.reload()
                page.locator("html[data-connected='yes']").wait_for(state="attached")
                run = api.get(
                    server.origin + "/api/v1/sessions",
                    params=scope_query(selected.model_dump(mode="json")),
                ).json()["items"][0]
                assert run["state"] == "STOPPED"  # loading/reconnecting did not START
                stopped = api.post(
                    server.origin + "/api/v1/agents/" + run["agent_id"] + "/stop",
                    headers=headers,
                    data={
                        "type": "agent.stop",
                        "request_id": "transport-stop",
                        "target": run["agent_id"],
                        "scope": run["scope"],
                    },
                )
                assert stopped.status == 200
                page.locator("html[data-refetch='yes']").wait_for(state="attached")
                calls = api.get(
                    server.origin + "/api/v1/llm-attempts",
                    params=scope_query(selected.model_dump(mode="json")),
                )
                assert calls.status == 200 and calls.json()["items"] == []
                assert api.get(server.origin + "/api/v1/opencode/provider").status == 404
                page.close()
            finally:
                browser.close()
