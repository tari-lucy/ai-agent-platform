"""Real local host + browser (OC-05): one initial access for the configured workplace set.

The host is prepared offline (recovery ledger, tickets, enable-workplace-files with a 7-day
automatic ceiling); the agent asked for one of the set earlier by a capability request. In the
panel the operator sees the set, prepares one plan, confirms once: both ordinary rights appear
and the earlier request is resolved by the owner. The agent stays STOPPED; synthetic data,
the .invalid model binding is never called.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_agent_application.storage import Grant, Owner
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import CapabilityRequest
from host_support import Server, free_port
from playwright.sync_api import expect
from quality_support import Model, prepared_host
from test_live import binding

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS

ROOT = Path(__file__).resolve().parents[2]


async def prepare(tmp_path, monkeypatch):
    host = await prepared_host(tmp_path, Model(), monkeypatch)
    await enable_recovery(
        host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger" / "events.sqlite"
    )
    await enable_tickets(host.root)
    await enable_workplace_files(host.root, ceiling_days=7)
    async with host.open():
        rt = host.runtime
        await rt.store.provision(
            host.scope,
            rights.host_grants(load_config(paths(host.root)))
            + (Grant(PRINCIPALS[Owner.ACTIVITY], Owner.ACTIVITY, read=True, write=True),),
        )
        await rt.tickets.raise_ticket(
            "need-skills",
            host.scope,
            "agent",
            "workplace-agent",
            TicketDraft(
                kind="capability_request",
                title="Нужна библиотека навыков",
                summary="Для своей работы.",
                attempted="Без неё.",
                decision_needed="Разрешить.",
                basis="workplace.skills",
                request=CapabilityRequest(capability_id="workplace.skills", constraints="—"),
            ),
            "Прошу.",
        )
    return host.root


def test_operator_grants_the_initial_set_with_one_decision(page, tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        root = executor.submit(lambda: asyncio.run(prepare(tmp_path, monkeypatch))).result()
    with Server(
        root,
        free_port(),
        tmp_path / "server.log",
        provider="live",
        panel_dir=ROOT / "apps/panel/dist",
        env_file=binding(tmp_path / "serve.env"),
    ) as server:
        page.goto(server.origin + "/#/workspace")
        page.get_by_label("Пароль", exact=True).fill(paths(root).password.read_text().strip())
        page.get_by_role("button", name="Войти", exact=True).click()
        members = [
            page.get_by_role("listitem", name=c)
            for c in ("workplace.knowledge", "workplace.skills")
        ]
        for member in members:
            expect(member.get_by_text("Не выдано")).to_be_visible(timeout=10000)

        page.get_by_role("button", name="Подготовить начальный доступ").click()
        plan = page.get_by_label("План последствий")
        expect(plan).to_contain_text("начальный доступ")
        expect(plan).to_contain_text("Возможность, добавленная позже")
        grant = plan.get_by_role("button", name="Выдать доступ")
        expect(grant).to_be_disabled()  # one explicit confirmation of the whole set
        plan.get_by_label("Я понимаю").check()
        grant.click()
        expect(
            page.get_by_text("Права выданы; прежние запросы этих возможностей закрыты.")
        ).to_be_visible()
        for member in members:
            expect(member.get_by_text("Разрешено")).to_be_visible()

        page.goto(server.origin + "/#/tickets")
        page.get_by_text("Нужна библиотека навыков").first.click()
        expect(
            page.get_by_text("Выдан начальный доступ рабочего места; запрос закрыт.")
        ).to_be_visible()
