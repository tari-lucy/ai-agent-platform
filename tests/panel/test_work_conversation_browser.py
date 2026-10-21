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
        expect(thread.get_by_text("Агент ответит после запуска", exact=False)).to_be_visible()
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
