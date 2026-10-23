"""Work conversations through the configured OpenCode capability, wired by ordinary serve.

Production init/build/ASGI, SQLite, ticket/permission/session owners and the shared budget. The
offline `enable-work-sessions` configuration replaces any programmatic install: serve wires the
session driver. The first right comes from one technical access ticket (raised by the platform,
not the character) and the operator's structured permission decision. Here the harness is the
scripted one over the shared gateway; `test_real_launcher.py` runs the same path with real
OpenCode. Synthetic data and provider only, no network, no paid calls.
"""

import asyncio

import pytest
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import area
from ai_agent_contracts.agent_workplace import Ticket
from session_support import ack, synthetic
from test_activity import WorkModel, fast, prepared, rows, until
from test_http import post
from test_library_host import decide
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host import work_sessions
from ai_agent_core.local_host.layout import HostError
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_access import TITLE

CAPABILITY = "workplace.opencode-session"
AREAS = ["projects", "inbox"]
IMAGE = "sha256:" + "a" * 64


async def configured(tmp_path, monkeypatch, model, *, ready=None):
    host = await prepared(tmp_path, monkeypatch, model)
    await enable_recovery(
        host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
    )
    with pytest.raises(HostError, match="WORK_SESSIONS_FILES_REQUIRED"):
        await enable_work_sessions(host.root, image_id=IMAGE, areas=AREAS, output_area="projects")
    await enable_workplace_files(host.root)
    # No Docker on the test machine: readiness is the only thing replaced, and only here.
    monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: ready)
    with pytest.raises(HostError, match="WORK_SESSIONS_LIMITS"):
        await enable_work_sessions(host.root, image_id=IMAGE, areas=AREAS, output_area="guide")
    if ready is None:
        await enable_work_sessions(
            host.root, image_id=IMAGE, areas=AREAS, output_area="projects", ceiling_days=7
        )
    return host


async def tickets(rt, scope):
    return await rows(rt, Ticket, area(scope), Owner.TICKETS)


async def stop(host, key):
    run = host.runtime.supervisor.snapshot
    response = await host.env.client.post(
        f"/api/v1/agents/{run.agent_id}/stop",
        json=dict(
            type="agent.stop",
            request_id=key,
            target=run.agent_id,
            scope=run.scope.model_dump(mode="json"),
        ),
        headers=host.env.headers,
    )
    assert response.status_code == 200, response.text


async def grant(host, ticket):
    await decide(
        host,
        "grant-work-sessions",
        dict(
            action="grant",
            capability_id=CAPABILITY,
            binding_id="opencode." + CAPABILITY,
            ticket_id=ticket.id,
            ticket_version=ticket.version,
            level="automatic",
            duration_seconds=86400,
            areas=AREAS,
            constraints="Рабочий контур в пределах областей.",
        ),
        ticket.version,
    )


async def answered(host, session_id, key):
    view = await until(
        lambda: transcript(host, session_id),
        lambda v: v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"],
        "draft " + key,
        seconds=40,
    )
    assert (await ack(host, view["pending_reply"], key)).status_code == 200


def test_system_ticket_grants_once_and_the_right_serves_every_conversation(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await configured(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            assert rt.session_capability == CAPABILITY and rt.session_unavailable is None
            synthetic(rt, CAPABILITY)  # serve wired real OpenCode; the scripted harness here

            first = (await open_session(host, "open-1", "Первый")).json()["resource_id"]
            assert (await say(host, "say-1", first, 1, "Рабочий вопрос.")).status_code == 200
            view = await transcript(host, first)
            assert view["waiting_reason"] == "access_required" and view["access_ticket_id"] is None
            await asyncio.sleep(1.0)
            assert not await tickets(rt, scope) and model.count() == 0  # STOPPED: nothing

            await host.start()
            view = await until(
                lambda: transcript(host, first),
                lambda v: v["waiting_reason"] == "access_requested",
                "access ticket",
                seconds=30,
            )
            ticket = await rt.tickets.get(scope, view["access_ticket_id"])
            assert ticket.kind == "capability_request" and ticket.title == TITLE
            assert ticket.request.capability_id == CAPABILITY and ticket.request.areas == AREAS
            assert [m.author for m in await rt.tickets.messages(scope, ticket.id)] == ["service"]

            # Repeated ticks and another waiting conversation merge into the same request.
            second = (await open_session(host, "open-2", "Второй")).json()["resource_id"]
            assert (await say(host, "say-2", second, 1, "Ещё вопрос.")).status_code == 200
            await asyncio.sleep(2.0)
            [same] = await tickets(rt, scope)
            assert same.id == ticket.id and same.version == ticket.version
            assert model.count() == 0

            # One decision: both stored messages are answered without being sent again.
            model.queues["activity"] += [{"reply": "Ответ."}, {"reply": "Ответ."}]
            await grant(host, ticket)
            await answered(host, first, "ack-1")
            await answered(host, second, "ack-2")
            assert model.count("activity") == 2
            assert (await transcript(host, first))["waiting_reason"] is None

            # A new conversation reuses the valid right: no new request.
            third = (await open_session(host, "open-3", "Третий")).json()["resource_id"]
            model.queues["activity"].append({"reply": "Снова ответ."})
            assert (await say(host, "say-3", third, 1, "Новый разговор.")).status_code == 200
            await answered(host, third, "ack-3")
            assert len(await tickets(rt, scope)) == 1 and model.count("activity") == 3

            await stop(host, "stop-1")
            fourth = (await open_session(host, "open-4", "После STOP")).json()["resource_id"]
            assert (await say(host, "say-4", fourth, 1, "Ждёт START.")).status_code == 200
            await asyncio.sleep(1.0)
            assert model.count("activity") == 3
        async with host.open():  # restart: STOPPED, the right is still valid, nothing runs
            rt = host.runtime
            fast(rt)
            synthetic(rt, CAPABILITY)
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await asyncio.sleep(1.0)
            assert model.count("activity") == 3
            model.queues["activity"].append({"reply": "После перезапуска."})
            await host.start()
            await answered(host, fourth, "ack-4")
            assert len(await tickets(rt, host.scope)) == 1

    asyncio.run(scenario())


def test_refusal_is_not_reopened_and_stop_while_waiting_runs_nothing(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await configured(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            synthetic(rt, CAPABILITY)
            session = (await open_session(host, "open", "Разговор")).json()["resource_id"]
            assert (await say(host, "say", session, 1, "Вопрос.")).status_code == 200
            await host.start()
            view = await until(
                lambda: transcript(host, session),
                lambda v: v["waiting_reason"] == "access_requested",
                "access ticket",
                seconds=30,
            )
            await stop(host, "stop-waiting")
            ticket = await rt.tickets.get(scope, view["access_ticket_id"])
            refused = await post(
                host.env,
                dict(
                    type="ticket.reply",
                    request_id="refuse",
                    target=ticket.id,
                    scope=scope.model_dump(mode="json"),
                    expected_version=ticket.version,
                    arguments=dict(
                        schema_id="ticket.reply-args",
                        data={"ticket_id": ticket.id, "body": "Нет.", "outcome": "close"},
                    ),
                ),
            )
            assert refused.status_code == 200, refused.text
            view = await transcript(host, session)
            assert view["waiting_reason"] == "access_refused"
            assert view["access_ticket_id"] == ticket.id
            closed = await rt.tickets.get(scope, ticket.id)
        async with host.open():
            rt = host.runtime
            fast(rt)
            synthetic(rt, CAPABILITY)
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await host.start()
            await asyncio.sleep(2.0)
            [kept] = await tickets(rt, host.scope)
            assert kept == closed and kept.state == "closed_unfulfilled"  # never reopened
            assert model.count() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["DOCKER_NOT_FOUND", "DOCKER_UNAVAILABLE"])
def test_not_ready_host_keeps_conversations_waiting_with_the_reason(tmp_path, monkeypatch, reason):
    async def scenario():
        model = WorkModel()
        host = await configured(tmp_path, monkeypatch, model)
        monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: reason)
        async with host.open():
            rt = host.runtime
            fast(rt)
            assert rt.session_harness is None and rt.session_unavailable == reason
            session = (await open_session(host, "open", "Разговор")).json()["resource_id"]
            assert (await say(host, "say", session, 1, "Вопрос.")).status_code == 200
            await host.start()
            await asyncio.sleep(1.5)
            view = await transcript(host, session)
            assert view["waiting_reason"] == "unavailable"
            assert view["waiting_detail"] == reason
            assert not await tickets(rt, host.scope) and model.count() == 0

    asyncio.run(scenario())


def test_cli_reports_fixed_codes_and_grants_nothing(tmp_path, monkeypatch, capsys):
    from ai_agent_core.local_host.cli import main

    async def scenario():
        return await prepared(tmp_path, monkeypatch, WorkModel())

    host = asyncio.run(scenario())
    assert main(["check-work-sessions", "--root", str(host.root)]) == 78
    assert "WORK_SESSIONS_NOT_ENABLED" in capsys.readouterr().err
    args = ["enable-work-sessions", "--root", str(host.root), "--image-id", IMAGE]
    assert main([*args, "--area", "projects", "--output-area", "projects"]) == 78
    assert "WORK_SESSIONS_FILES_REQUIRED" in capsys.readouterr().err
