"""Real local host + browser (AW-03): the operator sees the agent's skills and knowledge.

The host is prepared offline (recovery ledger, tickets, enable-workplace-files); the rights to
the two library areas are granted by the operator's structured decision; a skill, a note and a
hypothesis are written through the file owner by registered attempts (activity stands in, as
in tests/workplace_skills). The panel then reads /api/v1/workplace-library. Synthetic data.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_agent_application.agent_knowledge import NoteDraft
from ai_agent_application.storage import Grant, Owner
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import CapabilityRequest
from host_support import Server, free_port
from playwright.sync_api import expect
from quality_support import Model, prepared_host
from test_library_host import DRAFT, attempt, decide
from test_live import binding

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS

ROOT = Path(__file__).resolve().parents[2]
AREAS = {"workplace.skills": ["skills"], "workplace.knowledge": ["knowledge"]}


async def grant(host, capability: str) -> None:
    rt = host.runtime
    made = await rt.tickets.raise_ticket(
        "need-" + capability,
        host.scope,
        "agent",
        "agent",
        TicketDraft(
            kind="capability_request",
            title="Нужна " + capability,
            summary="Для своей работы.",
            attempted="Пробовала без неё.",
            decision_needed="Разрешить.",
            basis=capability,
            request=CapabilityRequest(capability_id=capability, constraints="Только своё."),
        ),
        "Прошу.",
    )
    ticket = await rt.tickets.get(host.scope, made.resource_id)
    await decide(
        host,
        "grant-" + capability,
        dict(
            action="grant",
            capability_id=capability,
            binding_id="workplace-library." + capability,
            ticket_id=ticket.id,
            ticket_version=ticket.version,
            level="automatic",
            duration_seconds=86400,
            areas=AREAS[capability],
            constraints="Только своё.",
        ),
        ticket.version,
    )


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
        for capability in AREAS:
            await grant(host, capability)
        place = rt.workplace
        await place.skills.save(await attempt(rt, 1), "s-1", DRAFT, b"1. Read.\n", None)
        for number, (kind, title) in enumerate(
            (("note", "Маяки: конспект"), ("hypothesis", "Маяки: догадка")), start=2
        ):
            await place.knowledge.write(
                await attempt(rt, number, "workplace.knowledge"),
                f"k-{number}",
                NoteDraft(f"{kind}-1", f"lighthouses/{kind}.md", title, kind),
                "Текст.",
                None,
            )
    return host.root


def test_operator_sees_skills_notes_and_hypotheses(page, tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        root = executor.submit(lambda: asyncio.run(prepare(tmp_path, monkeypatch))).result()
    # A synthetic .invalid binding: the agent stays STOPPED, so no model is ever called.
    with Server(
        root,
        free_port(),
        tmp_path / "server.log",
        provider="live",
        panel_dir=ROOT / "apps/panel/dist",
        env_file=binding(tmp_path / "serve.env"),
    ) as server:
        page.goto(server.origin + "/#/library")
        page.get_by_label("Пароль", exact=True).fill(paths(root).password.read_text().strip())
        page.get_by_role("button", name="Войти", exact=True).click()
        skill = page.get_by_role("listitem", name=DRAFT.title)
        expect(skill).to_be_visible(timeout=10000)
        expect(skill.get_by_text("Экспериментальный")).to_be_visible()
        expect(skill.get_by_text("Когда применять: " + DRAFT.applicability)).to_be_visible()
        note = page.get_by_role("listitem", name="Маяки: конспект")
        expect(note.get_by_text("Заметка", exact=True)).to_be_visible()
        expect(note.get_by_text("собственный текст агента", exact=False)).to_be_visible()
        guess = page.get_by_role("listitem", name="Маяки: догадка")
        expect(guess.get_by_text("Гипотеза", exact=True)).to_be_visible()
        expect(guess.get_by_text("предположение", exact=False)).to_be_visible()
        expect(page.get_by_text("Остановлен").first).to_be_visible()
