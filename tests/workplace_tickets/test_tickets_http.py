"""AW-01 over real HTTP: separate `serve` process, SQLite host, mock provider, synthetic data.

A platform service opens a ticket offline; the operator logs in, sees "needs my answer", reads
and answers while the agent is STOPPED; repeats replay, stale versions and foreign scopes are
refused; restart stays STOPPED and the answer waits for the agent.
"""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft, TicketService
from ai_agent_contracts.agent_workplace import CapabilityRequest
from ai_agent_contracts.api import Permission
from host_support import Server, command, free_port, init, scope_query

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

DRAFT = TicketDraft(
    kind="capability_request",
    title="Нужен доступ к справочнику",
    summary="Для задачи нужен общий справочник.",
    attempted="Искала в своих заметках.",
    decision_needed="Выдать справочник или отказать.",
    basis="reference.read",
    task_ids=("task-01",),
    request=CapabilityRequest(capability_id="reference.read", constraints="Только чтение."),
)


def enable(root: Path) -> None:
    done = subprocess.run(
        command("enable-tickets", "--root", str(root)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "agent stays STOPPED" in done.stdout


async def offline(root: Path, config: dict, action):
    store = SQLiteStore(paths(root).database, extensions=new_registry())
    try:
        scope = work_scope(config["workspace_id"], config["persona_id"], config["work_space_id"])
        service = TicketService(
            store,
            lambda: datetime.now(UTC),
            readers=rights.ticket_readers(),
            principal=PRINCIPALS[Owner.TICKETS],
        )
        return await action(service, store, scope)
    finally:
        await store.close()


def test_ticket_conversation_over_http_without_autostart(tmp_path):
    root = tmp_path / "host"
    done = init(root, "--port", "1024")
    assert done.returncode == 0, done.stderr
    config = json.loads((root / "host.json").read_text())
    assert config.get("workplace_tickets", False) is False  # off by default
    enable(root)
    assert json.loads((root / "host.json").read_text())["workplace_tickets"] is True

    async def raise_one(service, store, scope):
        receipt = await service.raise_ticket(
            "svc-1", scope, "service", "workplace", DRAFT, "Нужен справочник для задачи."
        )
        return receipt.resource_id

    ticket_id = asyncio.run(offline(root, config, raise_one))
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    base = {
        "workspace_id": config["workspace_id"],
        "persona_id": config["persona_id"],
        "memory_space_id": config["work_space_id"],
        "memory_mode": "work",
    }
    port = free_port()
    with Server(root, port, tmp_path / "serve.log") as server, server.client() as http:
        origin = {"Origin": server.origin}
        login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        assert login.status_code == 200, login.text
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def tickets():
            found = http.get("/api/v1/tickets", params=scope_query(base))
            assert found.status_code == 200, found.text
            return found.json()["items"]

        def post(body):
            return http.post("/api/v1/commands", json=body, headers=headers)

        def edit(kind, ticket, data, request_id, version=None, scope=base):
            return post(
                dict(
                    type=kind,
                    request_id=request_id,
                    target=ticket["id"],
                    scope=scope,
                    expected_version=version or ticket["version"],
                    arguments=dict(schema_id=kind + "-args", data=data),
                )
            )

        [ticket] = tickets()
        assert ticket["id"] == ticket_id and ticket["next_turn"] == "operator"
        assert ticket["operator_read_version"] < ticket["version"]
        run = http.get("/api/v1/sessions", params=scope_query(base)).json()["items"][0]
        assert run["state"] == "STOPPED"

        read = edit("ticket.read", ticket, {"ticket_id": ticket_id}, "op-read")
        assert read.status_code == 200, read.text
        [ticket] = tickets()
        assert ticket["operator_read_version"] == ticket["version"]

        body = {"ticket_id": ticket_id, "body": "Справочник выдам позже.", "outcome": "answer"}
        answered = edit("ticket.reply", ticket, body, "op-reply")
        assert answered.status_code == 200, answered.text
        # Lost response: the same command replays its receipt, no second message.
        again = edit("ticket.reply", ticket, body, "op-reply")
        assert again.status_code == 200 and again.json() == answered.json()
        stale = edit("ticket.reply", ticket, {**body, "outcome": "close"}, "op-stale")
        assert stale.status_code == 409, stale.text
        foreign = edit(
            "ticket.reply", ticket, body, "op-foreign", scope={**base, "memory_space_id": "nope"}
        )
        assert foreign.status_code in (403, 404), foreign.text
        messages = http.get("/api/v1/ticket-messages", params=scope_query(base)).json()["items"]
        assert [(m["author"], m["sequence"]) for m in messages] == [("service", 1), ("operator", 2)]
        [ticket] = tickets()
        assert (ticket["state"], ticket["next_turn"]) == ("in_progress", "agent")
        run = http.get("/api/v1/sessions", params=scope_query(base)).json()["items"][0]
        assert run["state"] == "STOPPED"  # the answer waits; nothing was started

    with Server(root, free_port(), tmp_path / "serve2.log") as server, server.client() as http:
        origin = {"Origin": server.origin}
        assert http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        run = http.get("/api/v1/sessions", params=scope_query(base)).json()["items"][0]
        assert run["state"] == "STOPPED"
        [ticket] = http.get("/api/v1/tickets", params=scope_query(base)).json()["items"]
        assert ticket["next_turn"] == "agent" and ticket["message_count"] == 2

    async def no_permission(service, store, scope):
        pending = await service.pending_for_agent(scope)
        rows = await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list(
            limit=200
        )
        return [t.id for t in pending], [p.capability_id for p in rows]

    pending, capabilities = asyncio.run(offline(root, config, no_permission))
    assert pending == [ticket_id]
    assert "reference.read" not in capabilities  # an answer in words grants nothing


def test_tickets_unavailable_without_offline_enable(tmp_path):
    root = tmp_path / "host"
    assert init(root, "--port", "1024").returncode == 0
    config = json.loads((root / "host.json").read_text())
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    base = {
        "workspace_id": config["workspace_id"],
        "persona_id": config["persona_id"],
        "memory_space_id": config["work_space_id"],
        "memory_mode": "work",
    }
    with Server(root, free_port(), tmp_path / "serve.log") as server, server.client() as http:
        origin = {"Origin": server.origin}
        login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}
        listed = http.get("/api/v1/tickets", params=scope_query(base))
        assert listed.status_code == 200 and listed.json()["items"] == []
        refused = http.post(
            "/api/v1/commands",
            json=dict(
                type="ticket.read",
                request_id="r-1",
                target="ticket-x",
                scope=base,
                expected_version=1,
                arguments=dict(schema_id="ticket.read-args", data={"ticket_id": "ticket-x"}),
            ),
            headers=headers,
        )
        assert refused.status_code >= 400 and "CAPABILITY_UNAVAILABLE" in refused.text
