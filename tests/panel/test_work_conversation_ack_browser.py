"""Linux headless browser (R5): the person answers in the work conversation; the panel ACKs it.

The production host app (`serve.build`) runs in-process under uvicorn with the built panel, so
the R1 session driver can use a synthetic harness over the shared model gateway (scripted
transport, no network, no paid calls). Checked in a real browser: the operator writes while
STOPPED (stored, waiting), START, the draft appears apart from history and the visible tab
acknowledges it once; the reply then is history (`character`), the waiting mark is gone, a
reload shows no duplicate, and STOP stays available. Real OpenCode is the Windows gate.
"""

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from ai_agent_adapters.llm import OpenAICompatibleProvider
from playwright.sync_api import expect
from session_support import install
from test_activity import WorkModel, fast, prepared
from test_live import binding

from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.local_host.serve import build

ROOT = Path(__file__).resolve().parents[2]
REPLY = "Начну с оглавления справочника, потом отмечу спорные места."


@pytest.fixture
def work_host(tmp_path, monkeypatch, page):
    model = WorkModel()
    with ThreadPoolExecutor(max_workers=1) as executor:
        host = executor.submit(lambda: asyncio.run(prepared(tmp_path, monkeypatch, model))).result()
    wire = httpx.AsyncClient(transport=httpx.MockTransport(model))
    monkeypatch.setattr(
        providers,
        "OpenAICompatibleProvider",
        lambda settings: OpenAICompatibleProvider(settings, wire),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    shared: dict = {}

    def worker():
        # The control owner is bound to its thread: build and serve in the same one.
        async def serve():
            app, close = build(
                paths(host.root),
                load_config(paths(host.root)),
                provider="live",
                port=port,
                panel_dir=ROOT / "apps/panel/dist",
                env_file=binding(tmp_path / "serve.env"),
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    access_log=False,
                    log_level="warning",
                    ws="websockets",
                    timeout_graceful_shutdown=5,
                )
            )
            shared.update(app=app, server=server, loop=asyncio.get_running_loop())
            try:
                await server.serve(sockets=[listener])
            finally:
                close()

        try:
            asyncio.run(serve())
        except BaseException as error:  # noqa: BLE001 - reported below
            shared["error"] = type(error).__name__

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while thread.is_alive() and time.monotonic() < deadline:
        if shared.get("server") and shared["server"].started:
            break
        time.sleep(0.02)
    assert shared.get("server") and shared["server"].started, shared.get("error")
    rt = shared["app"].state.runtime
    fast(rt)
    # The synthetic session harness, installed in the server's own loop (as R1's tests do).
    asyncio.run_coroutine_threadsafe(install(rt, host.scope), shared["loop"]).result(20)
    host.served = rt, shared["loop"]  # owner calls run in the server's own loop
    try:
        yield "http://127.0.0.1:" + str(port), host, model
    finally:
        page.goto("about:blank")
        shared["server"].should_exit = True
        thread.join(15)
        listener.close()
    assert not thread.is_alive() and "error" not in shared


def test_the_person_answers_in_the_panel_and_the_visible_tab_acknowledges(page, work_host):
    origin, host, model = work_host
    page.goto(origin + "/#/work")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()

    # STOPPED: the message is stored and waits; nothing calls the model.
    talk = page.get_by_label("Разговор о работе")
    talk.get_by_label("Новый разговор о работе").fill("Конспект справочника")
    talk.get_by_role("button", name="Начать разговор").click()
    thread = page.get_by_label("Разговор: Конспект справочника")
    thread.get_by_label("Сообщение агенту").fill("С чего начнёшь конспект?")
    thread.get_by_role("button", name="Отправить").click()
    messages = thread.get_by_role("list", name="Сообщения разговора")
    expect(messages.get_by_text("С чего начнёшь конспект?")).to_be_visible(timeout=10000)
    expect(thread.get_by_text("сохранено, ждёт исполнения", exact=False)).to_be_visible()
    assert model.count("activity") == 0

    # START: the person answers; the draft is shown apart from history and the visible tab
    # confirms receiving it exactly once; then it is history.
    model.queues["activity"].append({"reply": REPLY})
    page.get_by_role("button", name="START — запустить агента").click()
    expect(page.locator(".active-run")).to_contain_text("Работает", timeout=15000)
    expect(messages.get_by_text(REPLY)).to_be_visible(timeout=30000)
    expect(page.get_by_label("Ответ агента, ожидающий получения")).to_have_count(0)
    expect(thread.get_by_text("сохранено, ждёт исполнения", exact=False)).to_have_count(0)
    expect(thread.get_by_text("Ваш ход", exact=False)).to_be_visible()
    assert model.count("activity") == 1

    # Reload: the same two messages, no second reply; STOP is on this screen too.
    page.reload()
    thread = page.get_by_label("Разговор: Конспект справочника")
    messages = thread.get_by_role("list", name="Сообщения разговора")
    expect(messages.get_by_text(REPLY)).to_have_count(1, timeout=15000)
    expect(messages.get_by_role("listitem")).to_have_count(2)
    stop = page.get_by_role("button", name="STOP — остановить активного агента")
    expect(page.locator(".active-run")).to_contain_text("Работает", timeout=15000)
    stop.click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible(timeout=15000)
    assert model.count("activity") == 1


def test_a_revoked_message_stops_its_conversation_with_a_visible_reason(page, work_host):
    from ai_agent_application.storage import Owner
    from ai_agent_contracts.agent_sessions import WorkSession

    from ai_agent_core.m3_composition.dialogue import memory
    from ai_agent_core.m3_composition.state import PRINCIPALS, command
    from ai_agent_core.m3_composition.work_sessions import sessions

    origin, host, model = work_host
    rt, loop = host.served
    page.goto(origin + "/#/work")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    talk = page.get_by_label("Разговор о работе")
    talk.get_by_label("Новый разговор о работе").fill("Отозванный вопрос")
    talk.get_by_role("button", name="Начать разговор").click()
    thread = page.get_by_label("Разговор: Отозванный вопрос")
    thread.get_by_label("Сообщение агенту").fill("Синтетический вопрос, который будет отозван.")
    thread.get_by_role("button", name="Отправить").click()
    expect(thread.get_by_text("сохранено, ждёт исполнения", exact=False)).to_be_visible(
        timeout=10000
    )

    async def revoke():
        [session] = await rt.store.repository(
            WorkSession, PRINCIPALS[Owner.ACTIVITY], host.scope
        ).list(limit=10)
        [message] = await sessions(rt).messages(host.scope, session.id)
        await memory(rt).set_rights(
            command(host.scope, "revoke-browser"),
            message.source.source_id,
            message.source.version,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )

    asyncio.run_coroutine_threadsafe(revoke(), loop).result(20)
    # Even STOPPED, the panel says this message will not run (no START needed to learn it).
    page.reload()
    thread = page.get_by_label("Разговор: Отозванный вопрос")
    expect(thread.get_by_text("не будет исполнено", exact=False)).to_be_visible(timeout=15000)

    # START: the owner stops the conversation with the reason and how to continue; no model.
    page.get_by_role("button", name="START — запустить агента").click()
    expect(page.locator(".active-run")).to_contain_text("Работает", timeout=15000)
    expect(thread.get_by_role("status")).to_contain_text("начните новый разговор", timeout=30000)
    expect(thread.get_by_text("Чей ход: Разговор остановлен", exact=False)).to_be_visible()
    assert model.count("activity") == 0

    # The existing new-conversation path continues the work.
    talk.get_by_label("Новый разговор о работе").fill("Новый вопрос")
    talk.get_by_role("button", name="Начать разговор").click()
    expect(page.get_by_label("Разговор: Новый вопрос")).to_be_visible(timeout=10000)
