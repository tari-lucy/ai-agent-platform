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
        page.goto(origin + "/#/dialogue")
        page.get_by_label("Пароль", exact=True).fill(password)
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(
            page.get_by_role("button", name="START — запустить агента", exact=True)
        ).to_be_enabled()
        expect(page.get_by_role("button", name="Отправить", exact=True)).to_be_disabled()

    with Server(root, port, log, panel_dir=build) as server:
        login(server.origin)
        page.get_by_role("button", name="START — запустить агента", exact=True).click()
        expect(
            page.get_by_role("button", name="STOP — остановить активного агента", exact=True)
        ).to_be_enabled()
        expect(page.locator(".active-run")).to_contain_text("Работает")
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
        expect(
            page.get_by_role("button", name="START — запустить агента", exact=True)
        ).to_be_enabled()

        # Block only the start response after the real core accepts it. STOP must not wait.
        held = []

        def hold_start(route):
            if route.request.post_data_json.get("type") == "agent.start":
                held.append((route, route.fetch()))
            else:
                route.continue_()

        page.route("**/api/v1/commands", hold_start)
        page.get_by_role("button", name="START — запустить агента", exact=True).click()
        expect(
            page.get_by_role("button", name="STOP — остановить активного агента", exact=True)
        ).to_be_enabled()
        page.get_by_role("button", name="STOP — остановить активного агента", exact=True).click()
        expect(
            page.get_by_role("button", name="START — запустить агента", exact=True)
        ).to_be_enabled()
        assert held
        for route, response in held:
            route.fulfill(response=response)
        page.unroute("**/api/v1/commands", hold_start)
        expect(
            page.get_by_role("button", name="START — запустить агента", exact=True)
        ).to_be_enabled()
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
