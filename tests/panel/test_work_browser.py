"""Real local host + browser (AW-07): assign, abandon, reload and STOP from the work screen.

The host has activity enabled offline; the serve process uses a synthetic `.invalid` binding.
No task is left to run when START is pressed, so no model is ever called. Synthetic data.
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
GOAL = "Разобрать старые заметки о маяках."


def test_assign_abandon_reload_and_stop_on_the_work_screen(page, tmp_path, monkeypatch):
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
        expect(page.get_by_text("Агент не работает", exact=False)).to_be_visible(timeout=10000)
        expect(page.get_by_text("Деятельность сегодня: 0 из 20 вызовов модели")).to_be_visible()

        form = page.get_by_label("Новое поручение")
        form.get_by_role("button", name="Поручить").click()
        expect(form.get_by_role("alert")).to_contain_text("Опишите поручение.")
        form.get_by_label("Поручение").fill(GOAL)
        form.get_by_role("button", name="Поручить").click()
        expect(form.get_by_text("Агент возьмётся за него после запуска")).to_be_visible()
        working = page.get_by_role("list", name="В работе")
        card = working.get_by_role("listitem", name=GOAL)
        turn = card.get_by_text("Ход агента после запуска", exact=False)
        expect(turn).to_be_visible(timeout=10000)

        card.get_by_role("button", name="Отменить задачу").click()
        card.get_by_role("button", name="Подтвердить отмену").click()
        expect(card.get_by_role("alert")).to_contain_text("Укажите причину.")
        card.get_by_label("Причина отмены").fill("Передумала.")
        card.get_by_role("button", name="Подтвердить отмену").click()
        done = page.get_by_role("list", name="Завершённые").get_by_role("listitem", name=GOAL)
        expect(done.get_by_text("Отменено оператором: Передумала.")).to_be_visible(timeout=10000)

        # Reload: the owner's records come back, the agent is still STOPPED.
        page.reload()
        done = page.get_by_role("list", name="Завершённые").get_by_role("listitem", name=GOAL)
        expect(done).to_be_visible(timeout=10000)
        expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible()

        # The direct STOP is on this screen too; nothing is left to run, so no model call.
        page.get_by_role("button", name="START — запустить агента").click()
        stop = page.get_by_role("button", name="STOP — остановить активного агента")
        expect(stop).to_be_visible(timeout=15000)
        # STOP targets the exact run version the panel sees: wait for the started run first.
        expect(page.locator(".active-run")).to_contain_text("Работает", timeout=15000)
        stop.click()
        expect(page.get_by_role("button", name="START — запустить агента")).to_be_visible(
            timeout=15000
        )
