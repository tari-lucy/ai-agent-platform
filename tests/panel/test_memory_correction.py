"""Real local host + browser: a lost correction response cannot duplicate the edit."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from ai_agent_adapters.storage.database import SQLiteStore
from host_support import Server, free_port, init
from playwright.sync_api import expect
from test_memory_owner_correction import seed

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry

ROOT = Path(__file__).resolve().parents[2]


async def seed_host(root):
    host, registry = paths(root), new_registry()
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    store = SQLiteStore(host.database, extensions=registry)
    from datetime import UTC, datetime

    runtime = SimpleNamespace(
        store=store,
        registry=registry,
        readers=rights.record_readers(),
        supervisor=SimpleNamespace(clock=SimpleNamespace(utc=lambda: datetime.now(UTC))),
    )
    try:
        await seed(runtime, scope)
    finally:
        await store.close()


def test_correction_lost_reply_reuses_exact_command_and_shows_owner_revision(page, tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(lambda: asyncio.run(seed_host(root))).result()
    posted, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def lost_once(route):
        command = route.request.post_data_json
        if command.get("type") != "memory.correct":
            route.continue_()
            return
        posted.append(command)
        response = route.fetch()  # Real owner commits before the simulated network failure.
        assert response.status == 200
        if len(posted) == 1:
            route.abort()
        else:
            route.fulfill(response=response)

    page.route("**/api/v1/commands", lost_once)
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        page.goto(server.origin + "/#/memory")
        page.get_by_label("Пароль", exact=True).fill(
            (root / "secrets/operator-password.txt").read_text().strip()
        )
        page.get_by_role("button", name="Войти", exact=True).click()
        page.get_by_role("list", name="Список записей").get_by_role("button").filter(
            has_text="Утверждение"
        ).click()
        page.get_by_role("button", name="Исправить", exact=True).click()
        page.get_by_label("Исправленный текст", exact=True).fill("Иван живёт у реки.")
        page.get_by_label("Основание исправления", exact=True).fill("Оператор уточнил место.")
        page.get_by_label("Новая уверенность", exact=False).fill("0.3")
        page.get_by_role("button", name="Сохранить исправление", exact=True).click()
        check = page.get_by_role("button", name="Проверить исход", exact=True)
        expect(check).to_be_visible(timeout=10000)
        expect(page.get_by_label("Исправленный текст", exact=True)).to_be_disabled()
        # An intervening projection refresh must not discard the pending command.
        page.wait_for_timeout(500)
        check.click()
        expect(page.get_by_text("Исправление принято.", exact=False)).to_be_visible()
        detail = page.locator('[aria-label="Карточка записи"]')
        expect(detail).to_contain_text("Иван живёт у реки.")
        expect(detail).to_contain_text("Версия 2")
        expect(detail).to_contain_text("Оператор уточнил место.")
        assert len(posted) == 2 and posted[0] == posted[1]
        assert posted[0]["target"] == "correction-claim"
        assert not errors
        page.reload()
        page.get_by_role("list", name="Список записей").get_by_role("button").filter(
            has_text="Иван живёт у реки."
        ).click()
        expect(page.locator('[aria-label="Карточка записи"]')).to_contain_text("Версия 2")
        page.goto("about:blank")
