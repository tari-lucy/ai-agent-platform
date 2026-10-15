"""Actual browser decision and compensating rollback over owner SQLite/HTTP; no interception."""

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from access_support import PASSWORD
from development_support import prepared
from playwright.sync_api import expect
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def development_panel(tmp_path, page):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    shared = {}

    async def serve():
        async with prepared(tmp_path, origin=origin, realtime=True) as env:
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


def test_manual_decision_and_confirmed_rollback_persist_in_actual_runtime(page, development_panel):
    origin, shared = development_panel
    env = shared["env"]
    page.goto(origin + "/#/development")
    page.get_by_label("Пароль", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()
    page.get_by_role("tab", name="Изменения", exact=True).click()
    page.get_by_label("Список изменений").get_by_role("button").first.click()
    card = page.get_by_label("Карточка изменения")
    expect(card.get_by_role("button", name="Принять", exact=True)).to_be_disabled()
    card.get_by_label("Основание решения", exact=True).fill("Подтверждено вымышленным опытом.")
    card.get_by_role("button", name="Принять", exact=True).click()
    expect(card).to_contain_text("Стало: 0.6")
    expect(card).to_contain_text("Подтверждено вымышленным опытом.")
    card.get_by_role("button", name="Откатить", exact=True).click()
    card.get_by_role("button", name="Подготовить откат", exact=True).click()
    plan = card.get_by_label("План последствий")
    expect(plan).to_contain_text("0.6 → 0.5")
    execute = plan.get_by_role("button", name="Откатить изменение", exact=True)
    expect(execute).to_be_disabled()
    plan.get_by_label("Я понимаю", exact=False).check()
    execute.click()
    expect(card.get_by_label("Ход выполнения")).to_contain_text("Выполнено.")
    state = asyncio.run_coroutine_threadsafe(env.state(), shared["loop"]).result(timeout=5)
    assert state.values.data["values"]["openness"] == 0.5 and state.version == 4
    page.reload()
    page.get_by_role("tab", name="Изменения", exact=True).click()
    expect(page.get_by_label("Список изменений")).to_contain_text("openness: 0.6 → 0.5")
    assert not env.world.provider.calls
