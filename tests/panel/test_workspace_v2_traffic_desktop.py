"""Windows Edge: current traffic UI against a real isolated host, synthetic owner log."""

import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from ai_agent_adapters.sandbox.egress import EgressLimits
from host_support import Server, free_port
from playwright.sync_api import expect
from test_live import binding
from test_workspace_v2_desktop import ROOT, setup

from ai_agent_core.local_host.layout import paths
from ai_agent_core.m3_composition.work_session_egress import EgressSettings, log_writer

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HOME_IMAGE_ID") and os.getenv("AW_NATIVE_UI_DIR")),
    reason="explicit Desktop image and native bundle required",
)


def test_traffic_log_days_and_latest_200_in_edge(page, tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        host, target = executor.submit(lambda: asyncio.run(setup(tmp_path, monkeypatch))).result()
    try:
        settings = EgressSettings(
            "workplace.internet",
            os.environ["AW_HOME_IMAGE_ID"],
            EgressLimits(),
            host.root / "data/egress",
            5 << 20,
        )
        write = log_writer(settings, host.scope.persona_id, "synthetic-traffic")
        for i in range(205):
            write(
                dict(
                    method="CONNECT",
                    target=f"site-{i}.example:443",
                    status="ok",
                    up=1,
                    down=1,
                    seconds=0.1,
                )
            )
        write(
            dict(
                method="SOCKS",
                target="10.0.0.1:443",
                status="denied",
                reason="EGRESS_DESTINATION_DENIED",
                up=0,
                down=0,
                seconds=0.0,
            )
        )
        log_writer(settings, "other-persona", "foreign")(
            dict(method="GET", target="http://foreign.example/", status="ok", up=0, down=0)
        )
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        row = dict(
            at=yesterday + "T12:00:00+00:00",
            persona=host.scope.persona_id,
            attempt="yesterday",
            method="CONNECT",
            target="yesterday.example:443",
            status="ok",
            up=2,
            down=2,
            seconds=0.1,
        )
        (settings.log_dir / (yesterday + ".jsonl")).write_text(json.dumps(row) + "\n")
        with Server(
            host.root,
            free_port(),
            tmp_path / "serve.log",
            provider="live",
            panel_dir=ROOT / "apps/panel/dist",
            env_file=binding(tmp_path / "synthetic.env"),
        ) as server:
            page.goto(server.origin + "/#/work")
            page.get_by_label("Пароль", exact=True).fill(
                paths(host.root).password.read_text().strip()
            )
            page.get_by_role("button", name="Войти", exact=True).click()
            traffic = page.get_by_label("Интернет", exact=True)
            traffic.get_by_role("button", name="Показать журнал").click()
            expect(traffic.get_by_text("Показаны последние 200 запросов.")).to_be_visible(
                timeout=20000
            )
            expect(traffic.get_by_text("SOCKS 10.0.0.1:443", exact=True)).to_be_visible()
            expect(traffic.get_by_text("CONNECT site-204.example:443", exact=True)).to_be_visible()
            expect(traffic.get_by_text("http://foreign.example/", exact=False)).to_have_count(0)
            expect(traffic.get_by_role("button", name="Позже")).to_be_disabled()
            traffic.get_by_role("button", name="Раньше").click()
            expect(traffic.get_by_text("CONNECT yesterday.example:443", exact=True)).to_be_visible()
            traffic.get_by_role("button", name="Позже").click()
            expect(traffic.get_by_text("CONNECT site-204.example:443", exact=True)).to_be_visible()
    finally:
        result = subprocess.run(["docker", "volume", "rm", target], capture_output=True)
        assert result.returncode == 0
