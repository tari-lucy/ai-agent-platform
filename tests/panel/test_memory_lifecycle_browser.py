"""Prepared host/browser: source JSON download and deletion with a lost reply, no paid API."""

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from host_support import Server, command, free_port, init
from playwright.sync_api import expect
from test_memory_correction import seed_host

ROOT = Path(__file__).resolve().parents[2]


def test_real_export_download_and_delete_unknown_stays_visible(page, tmp_path):
    root = tmp_path / "host"
    assert init(root, "--operator-controls").returncode == 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(lambda: asyncio.run(seed_host(root))).result()
    prepared = subprocess.run(
        command(
            "enable-recovery",
            "--root",
            str(root),
            "--store",
            str(tmp_path / "backups"),
            "--ledger",
            str(tmp_path / "ledger/deletions.sqlite"),
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    attempts, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def lose_delete_reply(route):
        payload = route.request.post_data_json
        if payload.get("type") != "memory.delete.execute":
            route.continue_()
            return
        attempts.append(payload)
        response = route.fetch()
        assert response.status == 200
        if len(attempts) == 1:
            route.abort()
        else:
            route.fulfill(response=response)

    page.route("**/api/v1/commands", lose_delete_reply)
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        page.goto(server.origin + "/#/memory")
        page.get_by_label("Пароль", exact=True).fill(
            (root / "secrets/operator-password.txt").read_text().strip()
        )
        page.get_by_role("button", name="Войти", exact=True).click()
        source = (
            page.get_by_role("list", name="Список записей")
            .get_by_role("button")
            .filter(has_text="Вымышленный Иван живёт у озера.")
        )
        source.click()
        page.get_by_role("button", name="Экспорт", exact=True).click()
        page.get_by_role("button", name="Подготовить экспорт", exact=True).click()
        panel = page.get_by_label("План последствий")
        expect(panel).to_contain_text("Другие источники")
        panel.get_by_role("button", name="Экспортировать", exact=True).click()
        with page.expect_download() as downloading:
            page.get_by_role("button", name="Скачать JSON", exact=True).click()
        saved = tmp_path / "source-export.json"
        downloading.value.save_as(saved)
        exported = json.loads(saved.read_text(encoding="utf-8"))
        assert exported["schema_id"] == "memory.source-export"
        assert exported["source"]["content"] == "Вымышленный Иван живёт у озера."
        page.get_by_role("button", name="Удалить", exact=True).click()
        page.get_by_role("button", name="Подготовить удаление", exact=True).click()
        panel = page.get_by_label("План последствий")
        execute = panel.get_by_role("button", name="Удалить", exact=True)
        expect(execute).to_be_disabled()
        panel.get_by_label("Я понимаю, что удалённое восстановить нельзя").check()
        execute.click()
        check = page.get_by_role("button", name="Проверить исход", exact=True)
        expect(check).to_be_visible(timeout=15000)
        expect(source).to_have_count(0)
        expect(page.get_by_label("Операция с памятью")).to_be_visible()
        check.click()
        expect(page.get_by_label("Ход выполнения")).to_contain_text("Выполнено: Удаление")
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        page.reload()
        expect(page.get_by_text("Память пока пуста.", exact=True)).to_be_visible()
        assert not errors
        page.goto("about:blank")
