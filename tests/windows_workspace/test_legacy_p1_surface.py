"""P1 acceptance against an isolated Windows CLI host; no personal data/API calls."""

import asyncio
import sys
from pathlib import Path

import pytest
from host_support import Server, free_port, init
from playwright.sync_api import expect, sync_playwright

DIST = Path(__file__).resolve().parents[2] / "apps/panel/dist"


def test_real_cli_keeps_panel_but_retires_native_bridge(tmp_path):
    root, log = tmp_path / "host", tmp_path / "serve.log"
    assert init(root).returncode == 0
    with (
        Server(
            root,
            free_port(),
            log,
            panel_dir=DIST,
            opencode_ui_dir=tmp_path / "obsolete-bundle-not-present",
        ) as server,
        server.client() as client,
    ):
        assert client.get("/").status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"password": (root / "secrets/operator-password.txt").read_text().strip()},
            headers={"Origin": server.origin},
        )
        assert login.status_code == 200
        retired = (
            "/opencode",
            "/opencode/",
            "/opencode/old/session/fake",
            "/api/v1/opencode/agents",
        )
        statuses = {path: client.get(path).status_code for path in retired}
    assert "NOTICE:" in log.read_text(encoding="utf-8")
    assert statuses == dict.fromkeys(retired, 404)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Edge acceptance")
def test_no_harness_still_exposes_legacy_input_forms_in_edge(tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    with Server(root, free_port(), tmp_path / "serve.log", panel_dir=DIST) as server:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context()
            response = context.request.post(
                server.origin + "/api/v1/auth/login",
                data={"password": (root / "secrets/operator-password.txt").read_text().strip()},
                headers={"Origin": server.origin},
            )
            assert response.status == 200
            page = context.new_page()
            page.goto(server.origin + "/#/dialogue")
            expect(page.get_by_label("Сообщение персонажу", exact=True)).to_be_visible()
            page.goto(server.origin + "/#/work")
            expect(page.get_by_label("Поручение", exact=True)).to_be_visible()
            expect(page.get_by_label("Новый разговор о работе", exact=True)).to_be_visible()
            browser.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Edge acceptance")
def test_harness_enabled_panel_is_readonly_before_start_in_edge(tmp_path, monkeypatch):
    from ai_agent_application.laboratory.profile import REFERENCE_PROFILE

    from ai_agent_core.local_host import work_sessions
    from ai_agent_core.local_host.activity import configure_activity
    from ai_agent_core.local_host.harness import configure
    from ai_agent_core.local_host.init import initialise
    from ai_agent_core.local_host.layout import LiveConfig
    from ai_agent_core.local_host.recovery import enable_recovery
    from ai_agent_core.local_host.tickets import enable_tickets
    from ai_agent_core.local_host.workplace_files import enable_workplace_files

    root, native_port = tmp_path / "host", free_port()

    async def prepare():
        await initialise(
            root,
            profile_markdown=REFERENCE_PROFILE,
            operator_controls=True,
            live=LiveConfig(model_id="synthetic-p1", attempt_limit=300, attempt_period="utc_day"),
        )
        await enable_tickets(root)
        await enable_recovery(
            root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await configure_activity(root, enabled=True)
        await enable_workplace_files(root)
        # This browser test stays STOPPED. Only image readiness is synthetic here.
        monkeypatch.setattr(work_sessions, "check", lambda settings, **kwargs: None)
        await work_sessions.enable_work_sessions(
            root,
            image_id="sha256:" + "a" * 64,
            areas=["projects", "inbox"],
            output_area="projects",
        )
        configure(root, enabled=True, port=native_port, input_rate=16, output_rate=80)

    asyncio.run(prepare())
    env_file = tmp_path / "synthetic.env"
    env_file.write_text(
        "AI_PLATFORM_LLM_ENDPOINT=https://provider.invalid/v1\n"
        "AI_PLATFORM_LLM_API_KEY=synthetic-p1-key\n",
        encoding="utf-8",
    )
    with Server(
        root,
        free_port(),
        tmp_path / "serve.log",
        provider="live",
        panel_dir=DIST,
        env_file=env_file,
    ) as server:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context()
            assert (
                context.request.post(
                    server.origin + "/api/v1/auth/login",
                    data={"password": (root / "secrets/operator-password.txt").read_text().strip()},
                    headers={"Origin": server.origin},
                ).status
                == 200
            )
            page = context.new_page()
            page.goto(server.origin + "/#/dialogue")
            entry = page.get_by_role("link", name="Открыть OpenCode · не запущен", exact=True)
            expect(entry).to_have_attribute("href", f"http://127.0.0.1:{native_port}/")
            expect(page.get_by_label("Сообщение персонажу", exact=True)).to_have_count(0)
            page.goto(server.origin + "/#/work")
            expect(page.get_by_label("Поручение", exact=True)).to_have_count(0)
            expect(page.get_by_label("Новый разговор о работе", exact=True)).to_have_count(0)
            browser.close()
