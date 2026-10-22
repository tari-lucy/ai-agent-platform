"""Ticket reply -> existing session/job -> exact ACK; no paid calls or working host."""

import asyncio
import json

from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import Permission
from session_support import CAPABILITY, ack, install
from test_activity import WorkModel, fast, prepared, until
from test_http import post
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.work_session_workspace import Workspace


def test_ticket_answer_resumes_once_after_start_and_ack_survives_restart(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        ticket_id = None
        failures = []

        async def answer(rt, key):
            ticket = await rt.tickets.get(host.scope, ticket_id)
            response = await post(
                host.env,
                dict(
                    type="ticket.reply",
                    request_id=key,
                    target=ticket.id,
                    scope=host.scope.model_dump(mode="json"),
                    expected_version=ticket.version,
                    arguments=dict(
                        schema_id="ticket.reply-args",
                        data=dict(
                            ticket_id=ticket.id,
                            body="Synthetic operator answer: use existing tools.",
                            outcome="answer",
                        ),
                    ),
                ),
            )
            assert response.status_code == 200, response.text

        async with host.open():
            rt = host.runtime
            fast(rt)
            initial_perform = rt._perform

            async def initial_observed(ex):
                try:
                    return await initial_perform(ex)
                except Exception as error:
                    failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", initial_observed)
            await install(rt, host.scope)
            original_factory = rt.session_harness

            async def factory(ex, gateway, gate):
                harness = await original_factory(ex, gateway, gate)
                original_turn = harness.turn

                async def turn(admission, instruction):
                    nonlocal ticket_id
                    if ticket_id is None:
                        receipt = json.loads(
                            await Workspace(ex, gate).call(
                                gate,
                                WorkspaceRequest(
                                    "ask",
                                    "raise_ticket",
                                    dict(
                                        title="Synthetic tool question",
                                        body="Synthetic permission request",
                                        capability_id="missing.tool",
                                    ),
                                ),
                            )
                        )
                        ticket_id = receipt["ticket_id"]
                    return await original_turn(admission, instruction)

                harness.turn = turn
                return harness

            rt.session_harness = factory
            opened = await open_session(host, "ticket-session")
            identifier = opened.json()["resource_id"]
            assert (await say(host, "question", identifier, 1, "Synthetic task")).status_code == 200
            model.queues["activity"].append({"reply": "Synthetic clarification requested"})
            await host.start()

            async def view():
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            async def draft():
                return await until(
                    view,
                    lambda v: (
                        v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                    ),
                    "ticket response",
                    seconds=60,
                )

            first = await draft()
            assert (await ack(host, first["pending_reply"], "first-ack")).status_code == 200
            await asyncio.sleep(0.2)
            assert model.count("activity") == 1
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="ticket-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200, stopped.text
            await answer(rt, "ticket-answer")
            assert model.count("activity") == 1

        async with host.open():
            rt = host.runtime
            fast(rt)
            rt.session_harness, rt.session_capability = factory, CAPABILITY
            original = rt._perform

            async def observed(ex):
                try:
                    return await original(ex)
                except Exception as error:
                    failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", observed)
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count("activity") == 1
            model.queues["activity"].append({"reply": "Synthetic continuation after ticket answer"})
            await host.start()
            second = await draft()
            assert "Synthetic operator answer" in model.last("activity")["messages"][1]["content"]
            assert len(second["entries"]) == 2  # No fabricated operator chat message.
            assert second["processed_sequence"] == 2
            permissions = await rt.store.repository(
                Permission,
                PRINCIPALS[Owner.WORKSPACE],
                host.scope,
            ).list(limit=200)
            assert not any(p.capability_id == "missing.tool" for p in permissions)
            assert (await ack(host, second["pending_reply"], "second-ack")).status_code == 200
            assert (await ack(host, second["pending_reply"], "second-ack")).status_code == 200
            final = await view()
            assert len(final["entries"]) == 3 and final["processed_sequence"] == 3
            await asyncio.sleep(1.2)
            assert model.count("activity") == 2
            # A read receipt is not another answer, and a closed conversation stays closed.
            ticket = await rt.tickets.get(host.scope, ticket_id)
            read = await post(
                host.env,
                dict(
                    type="ticket.read",
                    request_id="read-ticket",
                    target=ticket.id,
                    scope=host.scope.model_dump(mode="json"),
                    expected_version=ticket.version,
                    arguments=dict(schema_id="ticket.read-args", data=dict(ticket_id=ticket.id)),
                ),
            )
            # An operator's own answer is already marked read; repeated read is a CAS refusal.
            assert read.status_code == 409, read.text
            closed = await post(
                host.env,
                dict(
                    type="work.session.close",
                    request_id="close",
                    target=identifier,
                    scope=host.scope.model_dump(mode="json"),
                    expected_version=final["session_version"],
                    arguments=dict(
                        schema_id="work.session.close-args", data=dict(session_id=identifier)
                    ),
                ),
            )
            assert closed.status_code == 200, closed.text
            await answer(rt, "late-answer")
            await asyncio.sleep(0.5)
            assert model.count("activity") == 2

    asyncio.run(scenario())
