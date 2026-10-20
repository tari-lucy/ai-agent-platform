"""AW-01 permission decisions over real HTTP: preview -> execute through the workspace owner.

Synthetic capability manifest/binding and a capability ticket; the host ceiling bounds the
grant. Checks the exact-version pins, replay, stale plans, revoke, that a granted right leaves
the binding pending and the agent STOPPED, and that no service/worker path reaches it.
"""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft, TicketService
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    CapabilityRequest,
    Ticket,
)
from ai_agent_contracts.api import Permission
from host_support import Server, command, free_port, init, scope_query

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import PermissionCeiling, load_config, paths, save_config
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, put

CAPABILITY = "reference.read"


def draft(summary: str) -> TicketDraft:
    return TicketDraft(
        kind="capability_request",
        title="Нужен справочник",
        summary=summary,
        attempted="Свои заметки.",
        decision_needed="Разрешить чтение справочника.",
        basis=CAPABILITY,
        request=CapabilityRequest(capability_id=CAPABILITY, constraints="Только чтение."),
    )


async def offline(root: Path, action):
    host, registry = paths(root), new_registry()
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    store = SQLiteStore(host.database, extensions=registry)
    try:
        tickets = TicketService(
            store,
            lambda: datetime.now(UTC),
            readers=rights.ticket_readers(),
            principal=PRINCIPALS[Owner.TICKETS],
        )
        return await action(store, registry, scope, tickets)
    finally:
        await store.close()


async def seed(store, registry, scope, tickets):
    now = datetime.now(UTC)
    base = dict(scope=scope, created_at=now, updated_at=now, version=1)
    manifest = CapabilityManifest(
        id="manifest-reference",
        capability_id=CAPABILITY,
        capability_version=1,
        summary="Чтение общего справочника.",
        instructions="Открывает выданные справочники только для чтения.",
        input_schema_id="reference.query",
        output_schema_id="reference.page",
        effect="none",
        areas=["reference"],
        network="none",
        runner_required=False,
        **base,
    )
    binding = CapabilityBinding(
        id="binding-reference",
        binding_id="binding-reference",
        capability_id=CAPABILITY,
        capability_version=1,
        adapter_id="reference-files",
        adapter_version="1",
        state="pending",
        **base,
    )
    readers = rights.record_readers()
    await put(store, registry, Owner.WORKSPACE, scope, "seed-manifest", manifest, None, readers)
    await put(store, registry, Owner.WORKSPACE, scope, "seed-binding", binding, None, readers)
    made = await tickets.raise_ticket(
        "seed-ticket", scope, "agent", "agent", draft("Нужен."), "Прошу."
    )
    return made.resource_id


async def state(store, registry, scope, tickets):
    permissions = await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list()
    binding = await store.repository(CapabilityBinding, PRINCIPALS[Owner.WORKSPACE], scope).get(
        "binding-reference"
    )
    [ticket] = await store.repository(Ticket, PRINCIPALS[Owner.TICKETS], scope).list()
    messages = await tickets.messages(scope, ticket.id)
    return permissions, binding, ticket, messages


def test_structured_grant_and_revoke_through_the_workspace_owner(tmp_path):
    root = tmp_path / "host"
    assert init(root, "--port", "1024", "--operator-controls").returncode == 0
    done = subprocess.run(
        command("enable-tickets", "--root", str(root)), capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    host = paths(root)
    config = load_config(host)
    save_config(
        host,
        config.model_copy(
            update={
                "workplace_permissions": {
                    CAPABILITY: PermissionCeiling(level="operator_decision", max_days=30)
                }
            }
        ),
    )
    ticket_id = asyncio.run(offline(root, seed))
    raw = json.loads((root / "host.json").read_text())
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    base = {
        "workspace_id": raw["workspace_id"],
        "persona_id": raw["persona_id"],
        "memory_space_id": raw["work_space_id"],
        "memory_mode": "work",
    }

    def run_server():
        return Server(root, free_port(), tmp_path / "serve.log")

    with run_server() as server, server.client() as http:
        origin = {"Origin": server.origin}
        login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def post(body, extra=None):
            return http.post("/api/v1/commands", json=body, headers=extra or headers)

        def ticket():
            return http.get("/api/v1/tickets", params=scope_query(base)).json()["items"][0]

        def preview(request_id, decision, version):
            return post(
                dict(
                    type="permission.decision.preview",
                    request_id=request_id,
                    target=CAPABILITY,
                    scope=base,
                    expected_version=version,
                    arguments=dict(schema_id="permission.decision-args", data=decision),
                )
            )

        def execute(request_id, plan_id):
            return post(
                dict(
                    type="permission.decision.execute",
                    request_id=request_id,
                    target=CAPABILITY,
                    scope=base,
                    expected_version=1,
                    arguments={"plan_id": plan_id, "plan_version": 1},
                )
            )

        current = ticket()
        grant = dict(
            action="grant",
            capability_id=CAPABILITY,
            binding_id="binding-reference",
            ticket_id=ticket_id,
            ticket_version=current["version"],
            level="operator_decision",
            duration_seconds=7 * 86400,
            areas=["reference"],
            constraints="Только чтение.",
        )
        # Above the host ceiling (level or duration) or outside the manifest areas: refused.
        for over in (
            {"level": "automatic"},
            {"duration_seconds": 31 * 86400},
            {"areas": ["knowledge"]},
        ):
            refused = preview("p-over-" + next(iter(over)), {**grant, **over}, current["version"])
            assert refused.status_code == 403, refused.text
        # A decision for an old ticket version is refused.
        old = preview("p-old", {**grant, "ticket_version": 99}, 99)
        assert old.status_code == 409, old.text

        planned = preview("p-1", grant, current["version"])
        assert planned.status_code == 200, planned.text
        plan_id = planned.json()["resource_id"]
        plan = http.get("/api/v1/plans/" + plan_id, params=scope_query(base))
        assert plan.status_code == 200 and "не запускает агента" in plan.json()["consequences"]
        assert "подключение ещё не готово" in plan.json()["consequences"]
        # A worker bearer token cannot use the panel command path at all.
        worker = post(
            dict(type="permission.decision.execute"),
            extra={"Authorization": "Bearer synthetic-worker-token"},
        )
        assert worker.status_code in (401, 403), worker.text

        executed = execute("x-1", plan_id)
        assert executed.status_code == 200, executed.text
        assert execute("x-1", plan_id).json() == executed.json()  # replay, no second write
        reused = execute("x-2", plan_id)
        assert reused.status_code == 409, reused.text
        run = http.get("/api/v1/sessions", params=scope_query(base)).json()["items"][0]
        assert run["state"] == "STOPPED"

        # The agent repeats its request: the ticket version moves and an older plan is stale.
        latest = ticket()["version"]
        second = preview("p-2", {**grant, "ticket_version": latest}, latest)
        assert second.status_code == 200, second.text
        stale_plan = second.json()["resource_id"]

    async def agent_repeats(store, registry, scope, tickets):
        await tickets.raise_ticket("again", scope, "agent", "agent", draft("Ещё раз."), "Повтор.")

    asyncio.run(offline(root, agent_repeats))

    with run_server() as server, server.client() as http:
        origin = {"Origin": server.origin}
        login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def post(body):
            return http.post("/api/v1/commands", json=body, headers=headers)

        stale = post(
            dict(
                type="permission.decision.execute",
                request_id="x-stale",
                target=CAPABILITY,
                scope=base,
                expected_version=1,
                arguments={"plan_id": stale_plan, "plan_version": 1},
            )
        )
        assert stale.status_code == 409, stale.text
        permission = http.get("/api/v1/permissions", params=scope_query(base)).json()["items"]
        [granted] = [p for p in permission if p["capability_id"] == CAPABILITY]
        revoke = dict(
            action="revoke",
            capability_id=CAPABILITY,
            binding_id="binding-reference",
            permission_id=granted["id"],
            permission_version=granted["version"],
        )
        planned = post(
            dict(
                type="permission.decision.preview",
                request_id="p-revoke",
                target=CAPABILITY,
                scope=base,
                expected_version=granted["version"],
                arguments=dict(schema_id="permission.decision-args", data=revoke),
            )
        )
        assert planned.status_code == 200, planned.text
        revoked = post(
            dict(
                type="permission.decision.execute",
                request_id="x-revoke",
                target=CAPABILITY,
                scope=base,
                expected_version=1,
                arguments={"plan_id": planned.json()["resource_id"], "plan_version": 1},
            )
        )
        assert revoked.status_code == 200, revoked.text

    permissions, binding, ticket_row, messages = asyncio.run(offline(root, state))
    [right] = [p for p in permissions if p.capability_id == CAPABILITY]
    assert (
        right.revoked_at is not None and right.version == 2 and right.level == "operator_decision"
    )
    assert right.principal_id == "workplace-agent" and right.author_id == "operator"
    assert binding.state == "pending"  # granting a right never marks the connection ready
    assert ticket_row.state == "waiting_reply"  # the agent's repeat keeps it open
    notes = [m.body for m in messages if m.author == "service"]
    assert any("Подключение ещё не готово" in body for body in notes)
    assert any("отозвал" in body for body in notes) is False  # revoke had no ticket attached


def test_decisions_unavailable_without_a_host_ceiling(tmp_path):
    root = tmp_path / "host"
    assert init(root, "--port", "1024", "--operator-controls").returncode == 0
    raw = json.loads((root / "host.json").read_text())
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    base = {
        "workspace_id": raw["workspace_id"],
        "persona_id": raw["persona_id"],
        "memory_space_id": raw["work_space_id"],
        "memory_mode": "work",
    }
    with Server(root, free_port(), tmp_path / "serve.log") as server, server.client() as http:
        origin = {"Origin": server.origin}
        login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
        headers = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}
        refused = http.post(
            "/api/v1/commands",
            json=dict(
                type="permission.decision.preview",
                request_id="p-1",
                target=CAPABILITY,
                scope=base,
                expected_version=1,
                arguments=dict(
                    schema_id="permission.decision-args",
                    data=dict(
                        action="grant",
                        capability_id=CAPABILITY,
                        binding_id="b",
                        ticket_id="t",
                        ticket_version=1,
                        level="prepare_only",
                        duration_seconds=3600,
                    ),
                ),
            ),
            headers=headers,
        )
        assert refused.status_code >= 400 and "CAPABILITY_UNAVAILABLE" in refused.text
