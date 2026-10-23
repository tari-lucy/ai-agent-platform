"""Windows R5: ordinary serve wires real OpenCode; operator grants access in the browser.

Only the LLM HTTP transport is scripted. Offline production configuration and serve install
the session driver; no test-only installer or pre-created Permission. The loopback HTTP/browser
fixture is shared with the existing ACK journey. No personal host or paid API is used.
"""

import asyncio
import os

import pytest
import test_work_conversation_ack_browser as support
from playwright.sync_api import expect
from test_real_launcher import REPLY, OpenCodeModel
from test_work_conversation_ack_browser import work_host as work_host

from ai_agent_core.local_host.layout import paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit real OpenCode image required"
)


@pytest.fixture
def configured_launcher(tmp_path, monkeypatch):
    prepare = support.prepared
    shared = {}

    async def prepared(directory, patch, model):
        host = await prepare(directory, patch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        await enable_work_sessions(
            host.root,
            image_id=os.environ["AW_OPENCODE_IMAGE_ID"],
            areas=["projects", "inbox"],
            output_area="projects",
            ceiling_days=7,
        )
        return host

    async def observe_only(rt, scope):
        # The fixture's old installer is replaced with observation, not another install.
        assert rt.session_harness is not None and rt.session_unavailable is None
        shared.update(rt=rt, loop=asyncio.get_running_loop())

    monkeypatch.setattr(support, "WorkModel", OpenCodeModel)
    monkeypatch.setattr(support, "prepared", prepared)
    monkeypatch.setattr(support, "install", observe_only)
    return shared


@pytest.fixture
def launcher_host(configured_launcher, work_host):
    return work_host, configured_launcher


def test_configured_opencode_answers_after_browser_permission_and_visible_ack(page, launcher_host):
    (origin, host, model), shared = launcher_host
    page.goto(origin + "/#/work")
    page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
    page.get_by_role("button", name="Войти", exact=True).click()
    talk = page.get_by_label("Разговор о работе")
    talk.get_by_label("Новый разговор о работе").fill("Проверка launcher")
    talk.get_by_role("button", name="Начать разговор").click()
    conversation = page.get_by_label("Разговор: Проверка launcher")
    conversation.get_by_label("Сообщение агенту").fill("Синтетический вопрос.")
    conversation.get_by_role("button", name="Отправить").click()
    expect(conversation.get_by_text("сохранено, ждёт исполнения", exact=False)).to_be_visible()
    assert model.count() == 0
    page.get_by_role("button", name="START — запустить агента").click()
    expect(conversation.get_by_text("решите запрос в обращениях", exact=False)).to_be_visible(
        timeout=30000
    )
    assert model.count() == 0

    page.goto(origin + "/#/tickets")
    page.get_by_role("list", name="Нужен мой ответ").get_by_role(
        "button", name="Для рабочего разговора требуется разрешение", exact=False
    ).click()
    # Capability, binding and versions come from current records; the default is automatic.
    form = page.get_by_label("Запрос доступа")
    expect(form.get_by_text("Подключение: проверено")).to_be_visible(timeout=15000)
    form.get_by_role("button", name="Разрешить…").click()
    form.get_by_label("Последствия решения").get_by_role(
        "button", name="Подтвердить выдачу доступа"
    ).click()
    expect(page.get_by_label("Карточка обращения").get_by_role("status")).to_contain_text(
        "Доступ выдан", timeout=15000
    )

    page.goto(origin + "/#/work")
    conversation = page.get_by_label("Разговор: Проверка launcher")
    messages = conversation.get_by_role("list", name="Сообщения разговора")
    expect(messages.get_by_text(REPLY, exact=True)).to_be_visible(timeout=180000)
    expect(page.get_by_label("Ответ агента, ожидающий получения")).to_have_count(0)
    expect(conversation.get_by_text("сохранено, ждёт исполнения", exact=False)).to_have_count(0)
    expect(messages.get_by_role("listitem")).to_have_count(2)
    assert model.count() == 1

    page.reload()
    expect(page.get_by_text(REPLY, exact=True)).to_have_count(1)
    page.get_by_role("button", name="STOP — остановить активного агента").click()
    expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible(timeout=15000)

    async def stopped():
        assert shared["rt"].supervisor.snapshot.state.value == "STOPPED"

    asyncio.run_coroutine_threadsafe(stopped(), shared["loop"]).result(10)
    assert model.count() == 1
