"""R5 Linux/headless journey on the shared main: one person, real owners, synthetic provider.

Production composition (build/ASGI), SQLite, memory/persona/tickets/activity/file owners,
recovery deletion ledger, private blob directory. The model is a scripted synthetic transport:
it proves the route, not quality. Not covered here (Windows/Docker gate of R5, Codex): the real
OpenCode harness, the sandbox runner (helper scripts) and the session driver's replies.

Journey: the operator talks to the person and assigns work while STOPPED (nothing runs) →
START → the person looks for a method, lacks the right → ticket → operator answer and
structured grant → saves and reads its own skill → result recorded as experience → its own
goal of a different kind by data → second ticket/grant → note → revoke closes text → STOP
holds → restart STOPPED → START continues. Every model call goes through the shared budget.
"""

import asyncio
from dataclasses import replace

from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkTask
from ai_agent_contracts.memory import Episode, Source
from test_activity import FAST, WorkModel, assign, fast, prepared, rows, task, until
from test_http import post, scope_params
from test_library_tools import KNOWLEDGE, SKILLS, grant, use
from test_runtime import ack, install
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command
from ai_agent_core.m3_composition.work_sessions import sessions
from ai_agent_core.m3_composition.workplace_experience import session_context


async def answer_ticket(host, ticket, text):
    response = await post(
        host.env,
        dict(
            type="ticket.reply",
            request_id="answer-" + ticket.id,
            target=ticket.id,
            scope=host.scope.model_dump(mode="json"),
            expected_version=ticket.version,
            arguments=dict(
                schema_id="ticket.reply-args",
                data={"ticket_id": ticket.id, "body": text, "outcome": "answer"},
            ),
        ),
    )
    assert response.status_code == 200, response.text
    return await host.runtime.tickets.get(host.scope, ticket.id)


async def waiting_ticket(host, task_id, known=()):
    found = await until(
        lambda: task(host, task_id),
        lambda t: t.state == "waiting" and t.waiting_ticket_ids[0] not in known,
        "capability ticket",
        seconds=30,
    )
    return await host.runtime.tickets.get(host.scope, found.waiting_ticket_ids[0])


async def stop(host):
    run = host.runtime.supervisor.snapshot
    response = await host.env.client.post(
        f"/api/v1/agents/{run.agent_id}/stop",
        json=dict(
            type="agent.stop",
            request_id="stop-journey",
            target=run.agent_id,
            scope=run.scope.model_dump(mode="json"),
        ),
        headers=host.env.headers,
    )
    assert response.status_code == 200, response.text


def test_linux_headless_journey_of_one_person(tmp_path, monkeypatch):
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
            rt.activity = replace(FAST, max_attempts_per_task=10)

            # 1. While STOPPED: the operator talks to the person and assigns; nothing runs.
            opened = await open_session(host, "open-1", "Метод наброска")
            session_id = opened.json()["resource_id"]
            said = await say(host, "say-1", session_id, 1, "Заведи себе метод наброска маяка.")
            assert said.status_code == 200, said.text
            assert (
                await say(host, "say-1", session_id, 1, "Заведи себе метод наброска маяка.")
            ).json() == said.json()
            shown = await transcript(host, session_id)
            assert shown["processed_sequence"] == 0 and len(shown["entries"]) == 1
            first = await assign(host, "assign-1", "Завести метод наброска маяка.")
            await asyncio.sleep(1.0)
            assert model.count() == 0 and rt.supervisor.snapshot.state.value == "STOPPED"

            find = use(SKILLS, "Поищу свой метод.", op="find", query="набросок маяка")
            save = use(
                SKILLS,
                "Метода нет, заведу свой.",
                op="save",
                skill_id="sketch",
                title="Набросок маяка",
                applicability="Быстро зарисовать маяк.",
                method="1. Контур башни.\n2. Свет.",
            )
            model.queues["activity"] += [
                find,  # no right yet: becomes a ticket
                find,
                save,
                use(SKILLS, "Перечитаю метод.", op="read", skill_id="sketch"),
                dict(action="complete", summary="Метод наброска заведён."),
            ]

            # 2. START: the missing capability becomes a ticket; only this task waits.
            await host.start()
            ticket = await waiting_ticket(host, first)
            assert ticket.request.capability_id == SKILLS
            ticket = await answer_ticket(host, ticket, "Да, заведи метод в своей библиотеке.")
            await grant(host, ticket, ["skills"])
            done = await until(
                lambda: task(host, first), lambda t: t.state == "done", "first task", seconds=40
            )
            assert done.outcome.startswith("Собственный вывод")
            assert [s.status for s in done.progress] == ["succeeded"] * 3

            # 3. Nothing assigned: the person picks its own, different occupation by data.
            rt.activity = replace(FAST, max_attempts_per_task=10, own_goals=True, own_goal_daily=1)
            write = use(
                KNOWLEDGE,
                "Запишу наблюдение.",
                op="write",
                file_id="note-light",
                path="notes/light.md",
                title="Свет маяка на закате",
                kind="note",
                body="На закате свет виден раньше башни.",
            )
            model.queues["activity"] += [
                dict(action="goal", goal="Записать наблюдение о свете маяка.", reason="Интерес."),
                write,  # a different capability: asked for separately
                write,
                dict(action="complete", summary="Наблюдение записано."),
            ]
            own = await until(
                lambda: rows(rt, WorkTask, scope),
                lambda found: any(t.origin == "own_goal" for t in found),
                "own goal",
                seconds=30,
            )
            own_id = next(t.id for t in own if t.origin == "own_goal")
            second = await waiting_ticket(host, own_id, known=(ticket.id,))
            assert second.request.capability_id == KNOWLEDGE
            await grant(host, second, ["knowledge"])
            await until(
                lambda: task(host, own_id), lambda t: t.state == "done", "own goal", seconds=40
            )

            # 4. Results are experience with receipts; conclusions stay interpretations.
            mem = PRINCIPALS[Owner.MEMORY]
            sources = await until(
                lambda: rt.store.repository(Source, mem, scope).list(limit=200),
                lambda found: sum(s.source_type == "workplace-conclusion" for s in found) == 2,
                "conclusions",
            )
            results = [s for s in sources if s.source_type == "workplace-result"]
            assert len(results) == 2 and {s.provenance for s in results} == {"observation"}
            episodes = [
                e
                for e in await rt.store.repository(Episode, mem, scope).list(limit=200)
                if e.operation_id.startswith("work-")
            ]
            assert len(episodes) == 2
            library = await host.env.client.get(
                "/api/v1/workplace-library", params=scope_params(scope)
            )
            assert [s["skill_id"] for s in library.json()["skills"]] == ["sketch"]
            assert "note-light" in {f["id"] for f in library.json()["files"]}

            # 5. Every model call went through the shared activity share; none were hidden.
            calls = model.count("activity")
            assert calls == 9
            status = await host.env.client.get(
                "/api/v1/private-api-status", params=scope_params(scope)
            )
            assert status.json()["activity_calls_used"] == calls

            # 5b. The person answers the operator's conversation message (R1 session driver,
            # synthetic harness over the shared gateway); it enters history only after ACK.
            await install(rt, scope)
            model.queues["activity"].append({"reply": "Метод наброска заведён, смотри библиотеку."})
            view = await until(
                lambda: transcript(host, session_id),
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "session draft",
                seconds=30,
            )
            assert [e["speaker"] for e in view["entries"]] == ["operator"]  # not history yet
            assert "смотри библиотеку" in view["pending_reply"]["text"]
            delivered = await ack(host, view["pending_reply"], "journey-ack")
            assert delivered.status_code == 200, delivered.text
            assert (
                await ack(host, view["pending_reply"], "journey-ack")
            ).json() == delivered.json()
            view = await transcript(host, session_id)
            assert [e["speaker"] for e in view["entries"]] == ["operator", "character"]
            assert view["pending_reply"] is None and view["processed_sequence"] == 2
            calls += 1
            assert model.count("activity") == calls
            replies = [
                s
                for s in await rt.store.repository(Source, mem, scope).list(limit=200)
                if s.source_type == "work-session-reply"
            ]
            assert len(replies) == 1

            # 6. Revoke: the conversation message's text and any context built on it close.
            message, _ = await sessions(rt).messages(scope, session_id)  # operator, reply
            port = session_context(rt)
            built = await port.build(scope, "набросок", [ticket.id])
            await memory(rt).set_rights(
                command(scope, "revoke-message"),
                message.source.source_id,
                message.source.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            entries = (await transcript(host, session_id))["entries"]
            assert entries[0]["withheld"] is True and entries[1]["withheld"] is False
            assert await port.still_valid(scope, built.refs)  # the message was not its basis

            # 7. STOP holds: new work is stored, nothing is submitted.
            await stop(host)
            third = await assign(host, "assign-3", "Сравнить два наброска.")
            await asyncio.sleep(1.5)
            assert model.count("activity") == calls
        # 8. Restart: STOPPED and silent; START continues with the stored task.
        async with host.open():
            rt = host.runtime
            fast(rt)
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await asyncio.sleep(1.0)
            assert model.count("activity") == calls
            model.queues["activity"].append(dict(action="complete", summary="Сравнила."))
            await host.start()
            await until(
                lambda: task(host, third), lambda t: t.state == "done", "after restart", seconds=30
            )
            assert model.count("activity") == calls + 1

    asyncio.run(scenario())
