"""Actual browser start/observation/cancel and persisted Job, with synthetic SQLite/HTTP."""

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from access_support import PASSWORD
from lab_control_support import prepared
from playwright.sync_api import expect
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def control_panel(tmp_path, monkeypatch, page):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    shared = {}

    async def serve():
        async with prepared(tmp_path, monkeypatch, origin=origin, realtime=True) as env:
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

            async def pump():
                while True:
                    await env.runtime.tick()
                    await asyncio.sleep(0.02)

            tick = asyncio.create_task(pump())
            try:
                await server.serve(sockets=[listener])
            finally:
                tick.cancel()
                from contextlib import suppress

                with suppress(asyncio.CancelledError):
                    await tick

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


def test_start_event_cancel_and_factual_job_status(page, control_panel):
    origin, shared = control_panel
    env = shared["env"]
    page.goto(origin + "/#/laboratory")
    page.get_by_label("Пароль", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role(
        "option", name=f"{env.lab_scope.persona_id} · ТЕСТ · control-test", exact=True
    ).click()
    page.get_by_label("Каталог сценариев", exact=True).get_by_role(
        "button", name="Выбрать", exact=True
    ).first.click()
    page.get_by_role("button", name="Запустить", exact=True).click()
    job = page.get_by_label("Лабораторное задание", exact=True)
    expect(job.get_by_role("status")).to_contain_text("RUNNING")
    job.get_by_label("Текст события", exact=True).fill("Вымышленное наблюдение.")
    job.get_by_role("button", name="Подать событие", exact=True).click()
    expect(job.get_by_text("Наблюдение сохранено в тестовой памяти.", exact=True)).to_be_visible()
    expect(job.get_by_text("Принято событий: 1 из 32", exact=True)).to_be_visible()
    job.get_by_role("button", name="Остановить задание", exact=True).click()
    expect(job.get_by_role("status")).to_contain_text("CANCELLED")

    async def state():
        return env.runtime.supervisor.snapshot.state.value

    assert asyncio.run_coroutine_threadsafe(state(), shared["loop"]).result(timeout=5) == "ACTIVE"
    page.reload()
    page.get_by_role("combobox", name="Просматриваемый персонаж и память", exact=True).click()
    page.get_by_role(
        "option", name=f"{env.lab_scope.persona_id} · ТЕСТ · control-test", exact=True
    ).click()
    expect(
        page.get_by_label("Лабораторное задание", exact=True).get_by_role("status")
    ).to_contain_text("CANCELLED")
    assert not env.world.provider.calls
