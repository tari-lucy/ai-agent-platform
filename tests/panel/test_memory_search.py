"""One real host/browser path: search an unloaded source without a provider call."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.states import Provenance
from host_support import Server, free_port, init
from playwright.sync_api import expect

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, command

ROOT = Path(__file__).resolve().parents[2]


async def seed(root):
    host = paths(root)
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    index = SQLiteRecallIndex(host.dialogue_index)
    try:
        memory = MemoryService(
            store,
            PRINCIPALS[Owner.MEMORY],
            registry,
            index=index,
            reader_access=rights.record_readers(),
        )
        rows = {}
        for number in range(55):
            word = f"указательметка{number}"
            content = f"Вымышленный {word}."
            result = await memory.register_source(
                command(scope, f"search-seed-{number}"),
                SourceDraft(
                    provenance=Provenance.OPERATOR_INPUT,
                    source_type="search-fixture",
                    adapter_id="fixture",
                    adapter_version="1",
                    occurred_at=datetime.now(UTC),
                    content=content,
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                    external_id=str(number),
                ),
            )
            rows[result.source_id] = (word, content)
        from ai_agent_contracts.memory import Source

        loaded = {
            s.id
            for s in await store.repository(Source, Principal("operator", "operator"), scope).list(
                limit=50, purpose="api_transfer"
            )
        }
        return next(
            (identifier, *value) for identifier, value in rows.items() if identifier not in loaded
        )
    finally:
        index.close()
        await store.close()


def test_search_unloaded_source_opens_authoritative_card_without_start(page, tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        identifier, word, content = executor.submit(lambda: asyncio.run(seed(root))).result()
    posts, errors = [], []
    page.on(
        "request", lambda request: posts.append(request.url) if request.method == "POST" else None
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        page.goto(server.origin + "/#/memory")
        page.get_by_label("Пароль", exact=True).fill(
            (root / "secrets/operator-password.txt").read_text().strip()
        )
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.get_by_role("heading", name="Найти в памяти", exact=True)).to_be_visible()
        expect(page.get_by_text(content, exact=True)).to_have_count(0)
        page.get_by_label("Что найти", exact=True).fill(word)
        page.get_by_role("button", name="Найти в памяти", exact=True).click()
        expect(page.get_by_text(content, exact=True)).to_have_count(1, timeout=15000)
        expect(page.get_by_text("Поиск по словам.", exact=False)).to_be_visible()
        page.get_by_role("button", name="Открыть источник", exact=True).click()
        expect(page.locator('[aria-label="Карточка записи"]')).to_contain_text(
            content, timeout=10000
        )
        assert word not in page.url
        assert sum(url.endswith("/api/v1/memory/recall") for url in posts) == 1
        assert not any(url.endswith("/api/v1/commands") for url in posts)
        assert not errors
        page.get_by_role("button", name="Очистить поиск", exact=True).click()
        expect(page.get_by_text("Результаты запроса", exact=False)).to_have_count(0)
        page.goto("about:blank")
