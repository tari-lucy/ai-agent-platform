"""Actual browser confirmation and persisted settings, with synthetic SQLite/HTTP."""

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from access_support import PASSWORD
from playwright.sync_api import expect
from starlette.staticfiles import StaticFiles
from transfer_support import prepared

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def transfer_panel(tmp_path, page):
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


def test_explicit_preview_confirmation_apply_and_persisted_history(page, transfer_panel):
    origin, shared = transfer_panel
    env = shared["env"]
    page.goto(origin + "/#/laboratory")
    page.get_by_label("Пароль", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()

    def select_test():
        page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
        page.get_by_role(
            "option", name=f"{env.source.persona_id} · ТЕСТ · transfer-test", exact=True
        ).click()

    select_test()
    form = page.get_by_label("Перенос настроек", exact=True)
    apply = form.get_by_role("button", name="Перенести настройки", exact=True)
    expect(apply).to_be_disabled()
    form.get_by_role("button", name="Предпросмотр переноса", exact=True).click()
    diff = form.get_by_label("Предпросмотр переноса", exact=True)
    expect(diff.get_by_text("0.5", exact=True)).to_be_visible()
    expect(diff.get_by_text("0.9", exact=True)).to_be_visible()
    expect(apply).to_be_disabled()
    form.get_by_label(
        "Подтверждаю перенос выбранных настроек в рабочую область", exact=True
    ).check()
    expect(apply).to_be_enabled()
    apply.click()
    expect(form.get_by_label("Состояние переноса", exact=True)).to_contain_text("Перенос завершён")
    current = asyncio.run_coroutine_threadsafe(
        env.runtime.state.gm(env.target), shared["loop"]
    ).result(timeout=5)
    assert current.intensity == 0.9
    page.reload()
    select_test()
    expect(form.get_by_text("Перенос завершён", exact=True)).to_be_visible()
    expect(apply).to_be_disabled()
    assert not env.world.provider.calls
