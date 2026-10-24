"""OC-05: one initial access decision for the configured workplace set, on a real host.

Production init/build/ASGI with the workplace files owner, which offline configures two
capabilities (skills, knowledge) with an automatic 7-day ceiling and ready bindings. Checked:
status of the set; a duration above the host ceiling is refused; one preview → one execute
writes both ordinary automatic Permissions atomically and closes the set's open capability
requests as resolved; replay returns the same receipt and another request on the executed
plan is refused; a plan made stale by a changed right writes nothing; a capability added
later does not inherit the decision; the agent stays STOPPED and no model is called.
"""

import asyncio
from datetime import UTC, datetime

from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft, area
from ai_agent_contracts.agent_workplace import CapabilityRequest, Ticket
from ai_agent_contracts.api import Permission
from test_activity import WorkModel, prepared
from test_http import post, scope_params

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS

SET = ["workplace.knowledge", "workplace.skills"]
WEEK = 7 * 86400


def preview_command(host, key, seconds, constraints=""):
    return dict(
        type="workplace.access.preview",
        request_id=key,
        target="initial-access",
        scope=host.scope.model_dump(mode="json"),
        expected_version=1,
        arguments=dict(
            schema_id="workplace.access-args",
            data={"duration_seconds": seconds, "constraints": constraints},
        ),
    )


def execute_command(host, key, plan_id):
    return dict(
        type="workplace.access.execute",
        request_id=key,
        target="initial-access",
        scope=host.scope.model_dump(mode="json"),
        expected_version=1,
        arguments={"plan_id": plan_id, "plan_version": 1},
    )


async def rights(rt, scope):
    rows = await rt.store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list(limit=50)
    # Workplace rights only (the host also holds its own platform permissions).
    return {row.capability_id: row for row in rows if row.capability_id.startswith("workplace.")}


def test_one_decision_grants_the_configured_set_atomically_and_closes_its_requests(
    tmp_path, monkeypatch
):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=7)
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            assert status["configured"] and status["ready"]
            assert status["max_duration_seconds"] == WEEK
            assert [m["capability_id"] for m in status["members"]] == SET
            assert not any(m["available"] for m in status["members"])

            # The agent asked for one of them earlier: the decision closes that request.
            raised = await rt.tickets.raise_ticket(
                "need-skills",
                scope,
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

            above = await post(env, preview_command(host, "too-long", WEEK + 86400))
            assert above.status_code == 403  # above the host ceiling: nothing planned

            planned = await post(env, preview_command(host, "plan-1", WEEK, "Только своё."))
            assert planned.status_code == 200, planned.text
            plan_id = planned.json()["resource_id"]
            plan = (
                await env.client.get(f"/api/v1/plans/{plan_id}", params=scope_params(scope))
            ).json()
            assert "начальный доступ" in plan["consequences"]
            assert "Возможность, добавленная позже" in plan["consequences"]
            assert not await rights(rt, scope)  # preview writes no right

            done = await post(env, execute_command(host, "grant-1", plan_id))
            assert done.status_code == 200, done.text
            granted = await rights(rt, scope)
            assert sorted(granted) == SET
            for row in granted.values():
                assert row.level == "automatic" and row.revoked_at is None
                assert abs((row.expires_at - datetime.now(UTC)).total_seconds() - WEEK) < 120
            assert (await post(env, execute_command(host, "grant-1", plan_id))).json() == (
                done.json()
            )  # replay: the same receipt, no second write
            # Replay needs only the durable receipt: a member later removed from the host
            # ceiling does not break it, and nothing is granted or extended again.
            configured = rt.permission_ceiling
            rt.permission_ceiling = {k: v for k, v in configured.items() if k != "workplace.skills"}
            replayed = await post(env, execute_command(host, "grant-1", plan_id))
            assert replayed.status_code == 200, replayed.text
            assert replayed.json() == done.json()
            rt.permission_ceiling = configured
            again = await post(env, execute_command(host, "grant-2", plan_id))
            assert again.status_code == 409  # an executed plan is not executed twice
            assert {r.version for r in (await rights(rt, scope)).values()} == {1}
            closed = await rt.tickets.get(scope, raised.resource_id)
            assert closed.state == "resolved"
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            assert all(m["available"] for m in status["members"])

            # A plan made stale by a changed right writes nothing (all or none).
            stale = (await post(env, preview_command(host, "plan-2", WEEK))).json()["resource_id"]
            skills = granted["workplace.skills"]
            revoked = await post(
                env,
                dict(
                    type="permission.decision.preview",
                    request_id="revoke-preview",
                    target="workplace.skills",
                    scope=scope.model_dump(mode="json"),
                    expected_version=skills.version,
                    arguments=dict(
                        schema_id="permission.decision-args",
                        data={
                            "action": "revoke",
                            "capability_id": "workplace.skills",
                            "binding_id": skills.binding_id,
                            "permission_id": skills.id,
                            "permission_version": skills.version,
                        },
                    ),
                ),
            )
            assert revoked.status_code == 200, revoked.text
            executed = await post(
                env,
                dict(
                    type="permission.decision.execute",
                    request_id="revoke",
                    target="workplace.skills",
                    scope=scope.model_dump(mode="json"),
                    expected_version=1,
                    arguments={"plan_id": revoked.json()["resource_id"], "plan_version": 1},
                ),
            )
            assert executed.status_code == 200, executed.text
            before = {k: v.version for k, v in (await rights(rt, scope)).items()}
            conflict = await post(env, execute_command(host, "grant-stale", stale))
            assert conflict.status_code == 409
            assert {k: v.version for k, v in (await rights(rt, scope)).items()} == before

            # A capability configured later does not inherit the executed decision.
            from ai_agent_core.m3_composition.permission_decisions import Ceiling

            rt.permission_ceiling = {
                **rt.permission_ceiling,
                "workplace.extra": Ceiling("automatic", WEEK),
            }
            assert "workplace.extra" not in await rights(rt, scope)

            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count() == 0
            assert not [
                t
                for t in await rt.store.repository(
                    Ticket, PRINCIPALS[Owner.TICKETS], area(scope)
                ).list()
                if t.state not in ("resolved", "closed_unfulfilled")
            ]

    asyncio.run(scenario())


def test_grounds_changed_after_the_check_refuse_the_commit(tmp_path, monkeypatch):
    """A binding changed between the pre-check and the write transaction writes nothing."""
    from ai_agent_contracts.agent_workplace import CapabilityBinding

    from ai_agent_core.m3_composition import initial_access
    from ai_agent_core.m3_composition.state import put

    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=7)
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            planned = await post(env, preview_command(host, "plan-1", WEEK))
            plan_id = planned.json()["resource_id"]
            # The pre-check sees the grounds as they were; the binding changes right after it.
            stale = await initial_access.members(rt, scope)
            owner = PRINCIPALS[Owner.WORKSPACE]
            rows = await rt.store.repository(CapabilityBinding, owner, scope).list(limit=50)
            row = next(b for b in rows if b.capability_id == "workplace.skills")
            bumped = row.model_copy(update={"version": row.version + 1})
            await put(rt.store, rt.registry, Owner.WORKSPACE, scope, "race", bumped, row.version)

            async def before_change(_rt, _scope):
                return stale

            monkeypatch.setattr(initial_access, "members", before_change)
            conflict = await post(env, execute_command(host, "grant-1", plan_id))
            assert conflict.status_code == 409, conflict.text
            assert not await rights(rt, scope)  # all or none: no member was granted
            assert model.count() == 0

    asyncio.run(scenario())
