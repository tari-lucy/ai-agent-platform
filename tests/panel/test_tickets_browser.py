"""Real local host + browser (AW-01): the operator answers a ticket while the agent is STOPPED.

A platform service opens a capability ticket offline; the panel shows it under "needs my answer",
marks it read, refuses an empty reply, saves the answer and moves the turn to the agent. The run
stays STOPPED and no permission is written. Synthetic data, mock provider.
"""

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft, TicketService
from ai_agent_contracts.agent_workplace import CapabilityRequest, Ticket
from ai_agent_contracts.api import Permission
from host_support import Server, command, free_port, init
from playwright.sync_api import expect

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

ROOT = Path(__file__).resolve().parents[2]
TITLE = "Нужен общий справочник"


async def owner(root, action):
    host = paths(root)
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    store = SQLiteStore(host.database, extensions=new_registry())
    try:
        service = TicketService(
            store,
            lambda: datetime.now(UTC),
            readers=rights.ticket_readers(),
            principal=PRINCIPALS[Owner.TICKETS],
        )
        return await action(service, store, scope)
    finally:
        await store.close()


async def seed(service, store, scope):
    draft = TicketDraft(
        kind="capability_request",
        title=TITLE,
        summary="Для конспекта нужен справочник.",
        attempted="Искала в своих заметках.",
        decision_needed="Выдать справочник или отказать.",
        basis="reference.read",
        request=CapabilityRequest(capability_id="reference.read", constraints="Только чтение."),
    )
    await service.raise_ticket("seed", scope, "service", "workplace", draft, "Справочника нет.")


async def final(service, store, scope):
    [ticket] = await store.repository(Ticket, PRINCIPALS[Owner.TICKETS], scope).list()
    permissions = await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list()
    return (
        ticket.state,
        ticket.next_turn,
        ticket.message_count,
        [p.capability_id for p in permissions],
    )


def in_thread(call):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(call)).result()


def test_operator_answers_a_ticket_in_the_panel_while_stopped(page, tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    enabled = subprocess.run(
        command("enable-tickets", "--root", str(root)), capture_output=True, text=True, timeout=120
    )
    assert enabled.returncode == 0, enabled.stderr
    in_thread(owner(root, seed))
    with Server(
        root, free_port(), tmp_path / "server.log", panel_dir=ROOT / "apps/panel/dist"
    ) as server:
        page.goto(server.origin + "/#/tickets")
        page.get_by_label("Пароль", exact=True).fill(
            (root / "secrets/operator-password.txt").read_text().strip()
        )
        page.get_by_role("button", name="Войти", exact=True).click()
        mine = page.get_by_role("list", name="Нужен мой ответ")
        expect(mine.get_by_text(TITLE)).to_be_visible(timeout=10000)
        expect(mine.get_by_text("новое")).to_be_visible()
        mine.get_by_role("button").first.click()
        card = page.get_by_label("Карточка обращения")
        expect(card.get_by_text("Запрошена возможность: reference.read")).to_be_visible()
        expect(card.get_by_text("Ответ текстом не выдаёт права")).to_be_visible()
        expect(card.get_by_text("Справочника нет.")).to_be_visible()
        # Opening it records the read: the unread mark disappears after the owner confirms.
        expect(mine.get_by_text("новое")).to_have_count(0, timeout=10000)
        expect(card.get_by_text("Агент остановлен")).to_be_visible()
        card.get_by_role("button", name="Отправить").click()
        expect(card.get_by_role("alert")).to_contain_text("Напишите сообщение.")
        card.get_by_label("Сообщение").fill("Выдам справочник позже, пока работай без него.")
        card.get_by_role("button", name="Отправить").click()
        working = page.get_by_role("list", name="В работе")
        expect(working.get_by_text(TITLE)).to_be_visible(timeout=10000)
        expect(card.get_by_text("Ход агента", exact=False)).to_be_visible()
        expect(card.get_by_text("Выдам справочник позже, пока работай без него.")).to_be_visible()
        expect(page.get_by_text("Остановлен").first).to_be_visible()
    state, turn, count, capabilities = in_thread(owner(root, final))
    assert (state, turn, count) == ("in_progress", "agent", 2)
    assert "reference.read" not in capabilities
