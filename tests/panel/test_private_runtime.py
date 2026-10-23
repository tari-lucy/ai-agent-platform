"""One live browser -> actual authenticated mock core path. No projection interception."""

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from access_support import PASSWORD
from ai_agent_adapters.llm.mock import Pause
from dev_support import answer
from integration_support import prepare_app
from playwright.sync_api import expect
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def private_panel(tmp_path, page):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    shared = {}

    async def serve():
        script = answer(reply="Настоящий HTTP, вымышленный ответ.")
        script.insert(2, Pause(0.5))
        env = await prepare_app(tmp_path, [script], origin=origin)
        env.app.mount("/", StaticFiles(directory=ROOT / "apps/panel/dist", html=True), name="panel")
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
        shared["server"], shared["env"] = server, env
        try:
            await server.serve(sockets=[listener])
        finally:
            env.index.close()
            await env.world.store.close()

    def worker():
        try:
            asyncio.run(serve())
        except BaseException as exc:
            shared["error"] = type(exc).__name__

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


def test_real_private_message_receipt_and_stop(page, private_panel):
    origin, shared = private_panel
    page.goto(origin)
    page.get_by_label("Пароль", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()
    page.get_by_role("link", name="Обзор и права").click()
    page.get_by_role("button", name="Запустить", exact=True).click()
    expect(page.get_by_text("Работает", exact=True)).to_be_visible()
    page.get_by_role("link", name="Диалог · временно", exact=True).click()
    page.get_by_label("Сообщение персонажу").fill("Вымышленное браузерное приветствие.")
    page.get_by_role("button", name="Отправить", exact=True).click()
    expect(page.get_by_role("list", name="Лента диалога")).to_contain_text(
        "Настоящий HTTP, вымышленный ответ.", timeout=15000
    )
    # The visible tab may acknowledge the shown reply by itself; the button is the manual path.
    # Either way exactly one receipt is recorded.
    receipt = page.get_by_role("button", name="Подтвердить получение приватного ответа", exact=True)
    try:
        receipt.click(timeout=3000)
    except Exception:  # noqa: BLE001 - already acknowledged automatically, button gone
        pass
    expect(page.get_by_role("list", name="Лента диалога")).to_contain_text(
        "Приватная панель подтвердила получение.", timeout=15000
    )
    page.get_by_role("button", name="STOP — остановить активного агента", exact=True).click()
    expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
    page.reload()
    expect(page.get_by_role("list", name="Лента диалога")).to_contain_text(
        "Настоящий HTTP, вымышленный ответ."
    )
    assert len(shared["env"].world.provider.calls) == 1
