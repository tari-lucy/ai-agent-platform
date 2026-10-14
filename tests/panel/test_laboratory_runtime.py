"""Real login, metadata and exact report bytes via the rendered laboratory button."""

import asyncio
import hashlib
import json
import socket
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn
from access_support import PASSWORD
from ai_agent_application.laboratory import LabRunner, lab_grants
from ai_agent_application.storage import Owner, RecordAccess
from playwright.sync_api import expect
from report_support import prepared
from starlette.staticfiles import StaticFiles

from ai_agent_core.m3_composition.state import put

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def laboratory_panel(tmp_path, page):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    shared = {}

    async def configure(env):
        rt = env.runtime
        child = env.world.scope.model_copy(
            update={"memory_mode": "test", "memory_space_id": "fresh-lab"}
        )
        await rt.store.provision(child, env.grants + lab_grants())
        rt.scopes += (child,)

        def forbidden(*args):
            raise AssertionError("Preparation invoked a factory")

        rt.lab = LabRunner(rt.store, forbidden, forbidden)
        for old in env.initial_permissions:
            await put(
                rt.store,
                rt.registry,
                Owner.WORKSPACE,
                env.world.scope,
                "refresh-" + old.id,
                old.model_copy(
                    update={
                        "version": old.version + 1,
                        "expires_at": env.world.clock() + timedelta(hours=1),
                    }
                ),
                old.version,
            )

    async def serve():
        async with prepared(tmp_path, origin=origin, realtime=True, configure=configure) as env:
            await put(
                env.world.store,
                env.runtime.registry,
                Owner.LABORATORY,
                env.scope,
                "save-incomplete-report",
                env.report.model_copy(update={"id": "report-pending", "report_artifact": None}),
                None,
                (RecordAccess("operator", api_transfer=True),),
            )
            env.app.mount(
                "/", StaticFiles(directory=ROOT / "apps/panel/dist", html=True), name="panel"
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    env.app,
                    host="127.0.0.1",
                    port=listener.getsockname()[1],
                    access_log=False,
                    log_level="warning",
                    ws="websockets",
                    ws_max_size=4096,
                    ws_max_queue=1,
                    ws_per_message_deflate=False,
                    timeout_graceful_shutdown=5,
                )
            )
            shared.update(server=server, env=env, loop=asyncio.get_running_loop())
            await server.serve(sockets=[listener])

    def worker():
        try:
            asyncio.run(serve())
        except BaseException as error:
            shared["error"] = type(error).__name__

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while thread.is_alive() and time.monotonic() < deadline:
        if shared.get("server") and shared["server"].started:
            break
        time.sleep(0.02)
    assert shared.get("server") and shared["server"].started, shared.get("error")
    try:
        yield origin, shared
    finally:
        page.goto("about:blank")
        shared["server"].should_exit = True
        thread.join(12)
        listener.close()
    assert not thread.is_alive() and "error" not in shared


def test_report_button_download_and_current_source_revoke(page, laboratory_panel):
    origin, shared = laboratory_panel
    page.goto(origin + "/#/laboratory")
    page.get_by_label("Пароль", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()
    env = shared["env"]
    page.get_by_role("button", name="Создать изолированную тестовую область", exact=True).click()
    expect(
        page.get_by_text("Тестовая область подготовлена: fresh-lab", exact=False)
    ).to_be_visible()
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role(
        "option", name=f"{env.scope.persona_id} · ТЕСТ · report-test", exact=True
    ).click()
    reports = page.get_by_label("Отчёты", exact=True)
    expect(reports.get_by_role("button", name="Скачать полный JSON")).to_have_count(2)
    expect(reports.get_by_role("button", name="Скачать полный JSON").nth(1)).to_be_disabled()
    download_button = reports.get_by_role("button", name="Скачать полный JSON").nth(0)
    with page.expect_download() as pending:
        download_button.click()
    downloaded = pending.value
    assert downloaded.failure() is None
    assert downloaded.suggested_filename == "laboratory-report.json"
    body = Path(downloaded.path()).read_bytes()
    assert hashlib.sha256(body).hexdigest() == env.report.report_artifact.checksum
    assert json.loads(body)["log"] == [{"detail": "synthetic full report"}]

    async def revoke():
        await env.world.store.provision(
            env.scope,
            tuple(
                replace(g, api_transfer=False)
                if g.principal.id == "operator" and g.owner == Owner.MEMORY
                else g
                for g in env.grants
            ),
        )

    asyncio.run_coroutine_threadsafe(revoke(), shared["loop"]).result(timeout=5)
    observed = []
    page.on("download", lambda value: observed.append(value))
    download_button.click()
    expect(page.get_by_role("alert").filter(has_text="Отчёт не скачан")).to_contain_text(
        "Доступ запрещён"
    )
    assert observed == []
    assert not env.world.provider.calls
