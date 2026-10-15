"""Real built panel against production init/serve subprocesses, generated secrets only."""

from pathlib import Path

from host_support import Server, free_port, init
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[2]


def test_local_host_browser_login_dialogue_stop_and_restart(page, tmp_path):
    root = tmp_path / "host"
    done = init(root)
    assert done.returncode == 0, done.stderr
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    port, log = free_port(), tmp_path / "host.log"
    build = ROOT / "apps/panel/dist"

    def login(origin):
        page.goto(origin + "/#/workspace")
        page.get_by_label("Пароль", exact=True).fill(password)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.get_by_role("link", name="Рабочее пространство", exact=True).click()
        expect(page.get_by_text("Остановлен", exact=True)).to_be_visible()

    with Server(root, port, log, panel_dir=build) as server:
        login(server.origin)
        page.get_by_role("button", name="Запустить", exact=True).click()
        expect(page.get_by_text("Работает", exact=True)).to_be_visible()
        page.get_by_role("link", name="Диалог", exact=True).click()
        page.get_by_label("Сообщение персонажу", exact=True).fill(
            "Вымышленное браузерное приветствие."
        )
        page.get_by_role("button", name="Отправить", exact=True).click()
        expect(page.get_by_role("list", name="Лента диалога")).to_contain_text(
            "Вымышленный ответ технического mock-режима.", timeout=20000
        )
        page.get_by_role(
            "button", name="Подтвердить получение приватного ответа", exact=True
        ).click()
        expect(page.get_by_role("list", name="Лента диалога")).to_contain_text(
            "Приватная панель подтвердила получение."
        )
        page.get_by_role("button", name="STOP — остановить активного агента", exact=True).click()
        expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
        page.goto("about:blank")
    with Server(root, port, log, panel_dir=build) as server:
        login(server.origin)
        page.get_by_role("link", name="Диалог", exact=True).click()
        expect(page.get_by_role("list", name="Лента диалога")).to_contain_text(
            "Вымышленный ответ технического mock-режима."
        )
        expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()
        page.goto("about:blank")
    assert password not in log.read_text(errors="replace")
