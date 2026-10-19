"""Real local host + browser: the operator closes an exact promise; the owner keeps its history."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.social import SocialService
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.control import Delivery
from ai_agent_contracts.memory import Promise
from ai_agent_contracts.states import DeliveryState, Provenance
from host_support import Server, free_port, init
from playwright.sync_api import expect

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, command, put

ROOT = Path(__file__).resolve().parents[2]
CONTENT = "Прислать вымышленный список книг о маяках."


async def seed_host(root):
    """A delivered reply with a promise, written by the memory, channel and social owners."""
    host, registry = paths(root), new_registry()
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    readers = rights.record_readers()
    store = SQLiteStore(host.database, extensions=registry)
    now = datetime.now(UTC)
    try:
        memory = MemoryService(store, PRINCIPALS[Owner.MEMORY], registry, reader_access=readers)
        made = await memory.register_source(
            command(scope, "seed-source"),
            SourceDraft(
                provenance=Provenance.OPERATOR_INPUT,
                source_type="operator-message",
                adapter_id="dialogue",
                adapter_version="1",
                occurred_at=now,
                content="Пришлёшь список книг о маяках?",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
                external_id="seed-message",
            ),
        )
        ref = SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )
        social = SocialService(store, PRINCIPALS[Owner.SOCIAL], registry, reader_access=readers)
        person = await social.observe_account(
            command(scope, "seed-person"), "private-panel", "operator", "оператор", ref
        )
        assert person is not None
        delivery = Delivery(
            id="seed-delivery",
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            utterance_id="seed-utterance",
            revision=1,
            channel_id="operator-panel",
            attempt_id="seed-attempt",
            status=DeliveryState.DELIVERED,
        )
        await put(store, registry, Owner.CHANNEL, scope, "seed-delivery", delivery, None, readers)
        await social.record_promise(
            command(scope, "seed-promise"),
            [person.person.id],
            "books",
            delivery_id="seed-delivery",
            utterance_id="seed-utterance",
            promise_key="books-list",
            content=CONTENT,
            completion_criterion="Список отправлен.",
            sources=[ref],
        )
    finally:
        await store.close()


async def history(root):
    host, registry = paths(root), new_registry()
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    store = SQLiteStore(host.database, extensions=registry)
    try:
        repo = store.repository(Promise, PRINCIPALS[Owner.SOCIAL], scope)
        current = (await repo.list())[0]
        first = await repo.get(current.id, version=1)
        return first.status, current.status, current.version, current.closure_result
    finally:
        await store.close()


def test_operator_withdraws_a_promise_in_the_people_tab(page, tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(lambda: asyncio.run(seed_host(root))).result()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        page.goto(server.origin + "/#/memory")
        page.get_by_label("Пароль", exact=True).fill(
            (root / "secrets/operator-password.txt").read_text().strip()
        )
        page.get_by_role("button", name="Войти", exact=True).click()
        page.get_by_role("tab", name="Люди", exact=True).click()
        page.get_by_role("list", name="Список людей").get_by_role("button").first.click()
        card = page.get_by_label("Карточка человека")
        expect(card.get_by_text(CONTENT)).to_be_visible(timeout=10000)
        card.get_by_role("button", name="Закрыть обещание…").click()
        card.get_by_role("button", name="Сохранить", exact=True).click()
        expect(card.get_by_role("alert")).to_contain_text("Укажите причину снятия.")
        card.get_by_label("Причина снятия").fill("Оператор передумал.")
        card.get_by_role("button", name="Сохранить", exact=True).click()
        expect(card.get_by_text("Итог: Оператор передумал.")).to_be_visible(timeout=10000)
        expect(card.get_by_text("Снято", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="Закрыть обещание…")).to_have_count(0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first, now, version, result = executor.submit(lambda: asyncio.run(history(root))).result()
    assert (first, now, version, result) == ("open", "withdrawn", 2, "Оператор передумал.")
    assert errors == []
