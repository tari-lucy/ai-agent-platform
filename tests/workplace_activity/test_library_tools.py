"""AW-05 + AW-03: the agent works with its library through permitted tool calls.

Production composition (build), SQLite, deletion ledger, private blob directory; a scripted
synthetic transport (no network, no paid calls). The agent asks for each library capability by
ticket, the operator grants it by structured decision, and the agent then saves, reads and
builds on its own skill. Checked: the route, rights per capability/area, lineage of a note
written after a read, and that a plan stays an own conclusion. Not model quality.
"""

import asyncio
from dataclasses import replace

from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import ManagedFile, Ticket, WorkAttempt
from test_activity import FAST, WorkModel, assign, fast, prepared, task, until
from test_http import scope_params
from test_library_host import decide

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS

SKILLS, KNOWLEDGE = "workplace.skills", "workplace.knowledge"
METHOD = "1. Прочитать текст целиком.\n2. Выписать тезисы.\n3. Проверить по тексту."


def use(capability: str, summary: str, **arguments: str) -> dict:
    return dict(
        action="use_capability", capability_id=capability, summary=summary, arguments=arguments
    )


async def grant(host, ticket: Ticket, areas: list[str]) -> None:
    assert ticket.request is not None
    capability = ticket.request.capability_id
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
            areas=areas,
            constraints="Только свои файлы.",
        ),
        ticket.version,
    )


def test_agent_asks_is_granted_and_builds_on_its_own_skill(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root,
            store_dir=tmp_path / "backups",
            ledger_path=tmp_path / "ledger" / "events.sqlite",
        )
        await enable_workplace_files(host.root, ceiling_days=7)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            # Seven steps for one task: above the default per-task cap, set by configuration.
            rt.activity = replace(FAST, max_attempts_per_task=8)
            assert set(rt.tool_executors) == {SKILLS, KNOWLEDGE}
            find = use(SKILLS, "Поищу свой метод.", op="find", query="конспект текста")
            write = use(
                KNOWLEDGE,
                "Запишу заметку по методу.",
                op="write",
                file_id="note-outline",
                path="notes/outline.md",
                title="Как я делаю конспект",
                kind="note",
                body="Сначала тезисы, потом проверка.",
            )
            # Each refused call is asked again after the grant: the agent repeats its choice.
            model.queues["activity"] += [
                find,
                find,
                use(
                    SKILLS,
                    "Метода нет, сохраню свой.",
                    op="save",
                    skill_id="outline",
                    title="Конспект текста",
                    applicability="Сжать длинный текст до тезисов.",
                    method=METHOD,
                ),
                use(SKILLS, "Перечитаю метод.", op="read", skill_id="outline"),
                write,
                write,
                dict(action="complete", summary="Метод и заметка готовы."),
            ]
            target = await assign(host, "assign-1", "Составить свой метод конспекта.")
            await host.start()

            # No right yet: the first library call becomes a capability ticket.
            waiting = await until(lambda: task(host, target), lambda t: t.state == "waiting", "ask")
            ticket = await rt.tickets.get(scope, waiting.waiting_ticket_ids[0])
            assert ticket.kind == "capability_request" and ticket.request.capability_id == SKILLS
            assert ticket.request.areas == ["skills"]
            await grant(host, ticket, ["skills"])

            # Granted: the agent repeats its plan, saves the method and reads it back; the
            # knowledge area is a different capability and is asked for separately.
            again = await until(
                lambda: task(host, target),
                lambda t: t.state == "waiting" and t.waiting_ticket_ids != [ticket.id],
                "knowledge ticket",
                seconds=30,
            )
            second = await rt.tickets.get(scope, again.waiting_ticket_ids[0])
            assert second.request.capability_id == KNOWLEDGE
            await grant(host, second, ["knowledge"])
            done = await until(
                lambda: task(host, target), lambda t: t.state == "done", "done", seconds=30
            )

            assert done.outcome.startswith("Собственный вывод")
            steps = [(s.capability_id, s.status) for s in done.progress]
            assert steps == [(SKILLS, "succeeded")] * 3 + [(KNOWLEDGE, "succeeded")]
            assert done.progress[0].summary == "Подходящих навыков нет."
            assert "Сохранён навык outline v1 (experimental)" in done.progress[1].summary
            assert METHOD in done.progress[2].summary  # what the agent read is in its context
            # The next step's prompt carried the earlier results.
            assert "Сохранён навык outline" in model.last("activity")["messages"][1]["content"]
            skill = await rt.workplace.skills.get(scope, "outline")
            assert (skill.status, skill.method.version) == ("experimental", 1)
            note = await rt.store.repository(ManagedFile, PRINCIPALS[Owner.CATALOGUE], scope).get(
                "note-outline"
            )
            # Written after reading the method: the file owner kept that lineage.
            assert note.derived_from == [skill.method] and note.provenance == "generated"
            assert {ref.artifact_id for ref in done.result_files} == {
                skill.method.artifact_id,
                "note-outline",
            }
            # Each tool attempt was bound to the capability it used before its IO.
            attempts = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], rt.supervisor.snapshot.scope
            ).list(limit=50)
            assert {a.capability_id for a in attempts} >= {SKILLS, KNOWLEDGE, "m3.activity"}
            assert model.count("activity") == 7  # one call per step, no hidden retries
            view = await host.env.client.get(
                "/api/v1/workplace-library", params=scope_params(scope)
            )
            assert [s["skill_id"] for s in view.json()["skills"]] == ["outline"]
            assert rt.supervisor.snapshot.state.value == "ACTIVE"

    asyncio.run(scenario())
