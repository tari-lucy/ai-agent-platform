"""Real init/serve and Edge: editable cap and versioned prices, no generation or paid API."""

import subprocess
from pathlib import Path

from host_support import Server, command, free_port, init
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[2]


def test_cap_and_tariff_survive_lost_replies_reload_and_restart(page, tmp_path):
    root = tmp_path / "host"
    prepared = init(
        root, "--operator-controls", "--live-model", "fixture-model", "--live-attempt-limit", "300"
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    result = subprocess.run(
        command(
            "configure-api-budget",
            "--root",
            str(root),
            "--attempt-limit",
            "300",
            "--period",
            "utc_day",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    binding = tmp_path / "fictional.env"
    binding.write_text(
        "AI_PLATFORM_LLM_ENDPOINT=https://fixture.invalid/v1\nAI_PLATFORM_LLM_API_KEY=fictional\n"
    )
    posted = {"llm.limits": [], "tariff.create": []}
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def lose_reply(route):
        body = route.request.post_data_json
        kind = body.get("type")
        if kind not in posted:
            route.continue_()
            return
        posted[kind].append(body)
        response = route.fetch()
        assert response.status == 200, response.text()
        if len(posted[kind]) == 1:
            route.abort()
        else:
            route.fulfill(response=response)

    page.route("**/api/v1/commands", lose_reply)

    def login(server):
        page.goto(server.origin + "/#/llm")
        page.get_by_label("Пароль", exact=True).fill(
            (root / "secrets/operator-password.txt").read_text().strip()
        )
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.get_by_role("heading", name="LLM и расходы", exact=True)).to_be_visible()

    for restart in (False, True):
        with Server(
            root,
            free_port(),
            tmp_path / "server.log",
            provider="live",
            panel_dir=ROOT / "apps/panel/dist",
            env_file=binding,
        ) as server:
            login(server)
            cap = page.get_by_label("Лимит API", exact=True)
            field = cap.get_by_label("Количество API-запросов")
            expect(field).to_have_value("350" if restart else "300")
            if not restart:
                field.fill("350")
                cap.get_by_role("button", name="Сохранить лимит", exact=True).click()
                retry = cap.get_by_role("button", name="Проверить сохранение лимита", exact=True)
                expect(retry).to_be_visible()
                expect(field).to_be_disabled()
                retry.click()
                expect(cap.get_by_role("status")).to_contain_text("Лимит сохранён")
                page.get_by_role("tab", name="Цены", exact=True).click()
                form = page.get_by_label("Новая версия тарифа", exact=True)
                form.get_by_label("Подключение", exact=True).click()
                page.get_by_role("option", name="Private API pilot", exact=True).click()
                form.get_by_label("ID модели").fill("fixture-model")
                form.get_by_label("Валюта").fill("USD")
                form.get_by_label("Вход (за единицу)").fill("0.20")
                form.get_by_label("Выход (за единицу)").fill("1")
                page.locator("#tariff-effective").fill("2020-01-01T00:00")
                form.get_by_role("button", name="Сохранить тариф", exact=True).click()
                retry = form.get_by_role("button", name="Проверить сохранение тарифа", exact=True)
                expect(retry).to_be_visible()
                retry.click()
                expect(form.get_by_role("status")).to_contain_text("Версия тарифа сохранена")
                assert all(len(rows) == 2 and rows[0] == rows[1] for rows in posted.values())
                page.reload()
            page.get_by_role("tab", name="Цены", exact=True).click()
            expect(page.get_by_text("v1 · с", exact=False)).to_be_visible()
            expect(
                page.get_by_role("button", name="START — запустить агента", exact=False)
            ).to_be_visible()
            page.goto("about:blank")
    assert not errors
