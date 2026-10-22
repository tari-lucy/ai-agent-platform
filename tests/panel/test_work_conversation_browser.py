"""Real local host + browser (R3): a work conversation with the person while it is STOPPED.

The host has activity enabled offline; serve uses a synthetic `.invalid` binding and the agent
is never started, so no model is called. Checked: start a conversation, write, whose turn,
reload, the event stream stays connected after the new event, close. Synthetic data.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from host_support import Server, free_port
from playwright.sync_api import expect
from test_activity import WorkModel, prepared
from test_live import binding

from ai_agent_core.local_host.layout import paths

ROOT = Path(__file__).resolve().parents[2]


def test_conversation_with_the_person_while_stopped(page, tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        host = executor.submit(
            lambda: asyncio.run(prepared(tmp_path, monkeypatch, WorkModel()))
        ).result()
    with Server(
        host.root,
        free_port(),
        tmp_path / "server.log",
        provider="live",
        panel_dir=ROOT / "apps/panel/dist",
        env_file=binding(tmp_path / "serve.env"),
    ) as server:
        page.goto(server.origin + "/#/work")
        page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
        page.get_by_role("button", name="Войти", exact=True).click()
        talk = page.get_by_label("Разговор о работе")
        talk.get_by_role("button", name="Начать разговор").click()
        expect(talk.get_by_role("alert")).to_contain_text("Назовите разговор.")
        talk.get_by_label("Новый разговор о работе").fill("Конспект справочника")
        talk.get_by_role("button", name="Начать разговор").click()
        thread = page.get_by_label("Разговор: Конспект справочника")
        expect(thread.get_by_text("Сообщений пока нет.")).to_be_visible(timeout=10000)

        thread.get_by_role("button", name="Отправить").click()
        expect(thread.get_by_role("alert")).to_contain_text("Напишите сообщение.")
        thread.get_by_label("Сообщение агенту").fill("Начни с оглавления, потом спорные места.")
        thread.get_by_role("button", name="Отправить").click()
        messages = thread.get_by_role("list", name="Сообщения разговора")
        expect(messages.get_by_text("Начни с оглавления, потом спорные места.")).to_be_visible(
            timeout=10000
        )
        expect(
            thread.get_by_text("исполнение начнётся только после запуска", exact=False)
        ).to_be_visible()
        expect(thread.get_by_text("сохранено, ждёт исполнения", exact=False)).to_be_visible()
        # The new event type did not break the event stream.
        expect(page.get_by_text("События подключены", exact=False).first).to_be_visible()

        page.reload()
        thread = page.get_by_label("Разговор: Конспект справочника")
        messages = thread.get_by_role("list", name="Сообщения разговора")
        expect(messages.get_by_text("Начни с оглавления, потом спорные места.")).to_be_visible(
            timeout=10000
        )
        expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()
        thread.get_by_role("button", name="Закрыть разговор").click()
        expect(thread.get_by_text("Разговор закрыт", exact=False)).to_be_visible(timeout=10000)
        expect(thread.get_by_label("Сообщение агенту")).to_have_count(0)


def card_task_id(host):
    """The only task of this synthetic host, read from its owner."""

    async def read():
        from ai_agent_adapters.storage.database import SQLiteStore
        from ai_agent_application.storage import Owner
        from ai_agent_contracts.agent_workplace import WorkTask

        from ai_agent_core.m3_composition import new_registry
        from ai_agent_core.m3_composition.state import PRINCIPALS

        store = SQLiteStore(paths(host.root).database, extensions=new_registry())
        try:
            [row] = await store.repository(WorkTask, PRINCIPALS[Owner.ACTIVITY], host.scope).list()
            return row.id
        finally:
            await store.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(read())).result()


def test_a_task_is_discussed_in_its_own_conversation(page, tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        host = executor.submit(
            lambda: asyncio.run(prepared(tmp_path, monkeypatch, WorkModel()))
        ).result()
    with Server(
        host.root,
        free_port(),
        tmp_path / "server.log",
        provider="live",
        panel_dir=ROOT / "apps/panel/dist",
        env_file=binding(tmp_path / "serve.env"),
    ) as server:
        page.goto(server.origin + "/#/work")
        page.get_by_label("Пароль", exact=True).fill(paths(host.root).password.read_text().strip())
        page.get_by_role("button", name="Войти", exact=True).click()
        form = page.get_by_label("Новое поручение")
        form.get_by_label("Поручение").fill("Разобрать заметки о маяках.")
        form.get_by_role("button", name="Поручить").click()
        card = page.get_by_role("list", name="В работе").get_by_role(
            "listitem", name="Разобрать заметки о маяках."
        )
        card.get_by_role("button", name="Обсудить с агентом").click(timeout=10000)
        thread = page.get_by_label("Разговор: Обсуждение: Разобрать заметки о маяках.")
        expect(thread).to_be_visible(timeout=10000)
        # The second click opens the same conversation; no second one is created.
        expect(card.get_by_role("button", name="Открыть разговор")).to_be_visible(timeout=10000)
        card.get_by_role("button", name="Открыть разговор").click()
        expect(thread).to_be_visible()
        query = dict(
            zip(
                ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
                host.scope.partition,
                strict=True,
            )
        )
        listed = page.request.get(server.origin + "/api/v1/work-sessions", params=query)
        assert [row["task_ids"] for row in listed.json()["items"]] == [[card_task_id(host)]]
