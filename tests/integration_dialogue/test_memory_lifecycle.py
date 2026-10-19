"""Owner/HTTP plans and real independent ledger: no paid calls or working data."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_application.memory import ClaimDraft
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.memory import Claim, Source, Summary
from ai_agent_contracts.panel import MemoryPlanProof
from integration_support import prepared
from test_http import scope_params, start
from test_memory_owner_correction import seed

from ai_agent_core.m3_composition.state import PRINCIPALS, command, put


@asynccontextmanager
async def world(tmp_path, *, purgers=()):
    async with prepared(tmp_path) as env:
        rt, scope = env.runtime, env.world.scope
        # Explicit fixture matrix matching prepared --operator-controls, never runtime widening.
        await rt.store.provision(
            scope,
            (
                *env.grants,
                Grant(PRINCIPALS[Owner.GATEWAY], Owner.MEMORY, read=True),
                *(
                    Grant(Principal("operator", "operator"), owner, read=True, api_transfer=True)
                    for owner in (
                        Owner.WORKSPACE,
                        Owner.PERSONA,
                        Owner.MEMORY,
                        Owner.GATEWAY,
                        Owner.CHANNEL,
                        Owner.JOBS,
                        Owner.LLM,
                        Owner.GM,
                    )
                ),
            ),
        )
        memory, source_id = await seed(rt, scope)
        memory.index = env.index
        await memory.reindex_scope(scope)
        physical = SQLitePhysicalStore(rt.store)
        await physical.install()
        ledger = SQLiteDeletionLedger(tmp_path / "independent-ledger.sqlite")
        rt.retention = RetentionService(
            rt.store,
            PRINCIPALS[Owner.MEMORY],
            memory,
            physical,
            ledger,
            index=env.index,
            purgers=purgers,
        )
        env.memory, env.source_id, env.ledger = memory, source_id, ledger
        try:
            yield env
        finally:
            ledger.close()


async def post(env, body):
    return await env.client.post("/api/v1/commands", headers=env.headers, json=body)


def preview_body(env, operation):
    return dict(
        type="memory." + operation + ".preview",
        request_id="preview-" + uuid4().hex,
        scope=env.world.scope.model_dump(mode="json"),
        target=env.source_id,
        expected_version=1,
        arguments=dict(
            schema_id="memory." + operation + "-args", data={"source_ids": [env.source_id]}
        ),
    )


async def plan(env, operation):
    payload = preview_body(env, operation)
    result = await post(env, payload)
    assert result.status_code == 200, result.text
    identifier = result.json()["resource_id"]
    view = await env.client.get("/api/v1/plans/" + identifier, params=scope_params(env.world.scope))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["dependencies"] == [{"resource_id": env.source_id, "version": 1}]
    return dict(
        type="memory." + operation + ".execute",
        request_id="execute-" + uuid4().hex,
        scope=env.world.scope.model_dump(mode="json"),
        target=env.source_id,
        expected_version=1,
        arguments=dict(plan_id=identifier, plan_version=1),
    )


def test_export_then_delete_exact_source_and_no_resurrection(tmp_path):
    async def scenario():
        async with world(tmp_path) as env:
            scope, rt = env.world.scope, env.runtime
            exported = await plan(env, "export")
            done = await post(env, exported)
            assert done.status_code == 200, done.text
            assert (await post(env, exported)).json() == done.json()
            download = "/api/v1/memory/exports/" + done.json()["operation_id"] + "/content"
            response = await env.client.get(download, params=scope_params(scope))
            assert response.status_code == 200, response.text
            assert response.json()["schema_id"] == "memory.source-export"
            assert response.json()["source"]["id"] == env.source_id
            assert response.headers["cache-control"] == "no-store"
            assert "claims" not in response.json() and "secrets" not in response.json()
            # One-use plan cannot be silently consumed by a second command.
            other = {**exported, "request_id": "other-execute"}
            assert (await post(env, other)).json()["code"] == "IDEMPOTENCY_CONFLICT"
            deletion = await plan(env, "delete")
            complete = await post(env, deletion)
            assert complete.status_code == 200, complete.text
            assert complete.json()["status"] == "completed"
            assert (await post(env, deletion)).json() == complete.json()
            assert (await env.client.get(download, params=scope_params(scope))).status_code == 403
            for model, identifier in (
                (Source, env.source_id),
                (Claim, "correction-claim"),
                (Summary, "correction-summary"),
            ):
                assert (
                    await rt.store.repository(model, PRINCIPALS[Owner.MEMORY], scope).get(
                        identifier
                    )
                    is None
                )
            entries = await env.ledger.entries(scope)
            assert len(entries) == 1 and entries[0].reason == "operator_delete"
            assert entries[0].purged_at is not None
            assert env.source_id not in await env.index.inventory(scope)
            # SQLite physical erasure includes old revisions, free pages and WAL, not just UI flags.
            database = tmp_path
            marker = "Вымышленный Иван живёт у озера.".encode()
            for path in database.rglob("*.sqlite*"):
                assert marker not in path.read_bytes()
            assert not env.world.provider.calls
            assert rt.supervisor.snapshot.state.value == "STOPPED"

    asyncio.run(scenario())


def test_pending_purge_reconciles_same_operation_and_same_plan(tmp_path):
    class Purger:
        broken = True

        async def purge_sources(self, scope, source_ids):
            if self.broken:
                raise PermissionError("synthetic failure")

    async def scenario():
        purger = Purger()
        async with world(tmp_path, purgers=(purger,)) as env:
            deletion = await plan(env, "delete")
            first = await post(env, deletion)
            assert first.status_code == 200 and first.json()["status"] == "waiting"
            progress = await env.client.get(
                "/api/v1/owner-operations/" + first.json()["operation_id"],
                params=scope_params(env.world.scope),
            )
            assert progress.json()["completed_steps"] == ["Источник заблокирован"]
            assert progress.json()["pending_steps"]
            assert await env.memory.get_source(env.world.scope, env.source_id) is None
            assert (await env.ledger.entries(env.world.scope))[0].purged_at is None
            purger.broken = False
            retry = await post(env, deletion)
            assert retry.status_code == 200 and retry.json()["status"] == "completed", retry.text
            assert (await post(env, deletion)).json() == retry.json()
            assert (await env.ledger.entries(env.world.scope))[0].purged_at is not None
            assert not env.world.provider.calls

    asyncio.run(scenario())


def test_changed_region_rights_expiry_and_active_are_refused(tmp_path):
    async def scenario():
        async with world(tmp_path) as env:
            deletion = await plan(env, "delete")
            original = await env.runtime.store.repository(
                Claim, PRINCIPALS[Owner.MEMORY], env.world.scope
            ).get("correction-claim")
            await put(
                env.runtime.store,
                env.runtime.registry,
                Owner.MEMORY,
                original.scope,
                "revise-dependent-fixture",
                original.model_copy(
                    update={
                        "version": 2,
                        "updated_at": env.runtime.supervisor.clock.utc(),
                        "content": "Уточнённая вымышленная интерпретация.",
                    }
                ),
                1,
            )
            stale_version = await post(env, deletion)
            assert (
                stale_version.status_code == 409
                and stale_version.json()["code"] == "VERSION_CONFLICT"
            )
            deletion = await plan(env, "delete")
            await env.memory.record_claim(
                command(env.world.scope, "new-dependent"),
                ClaimDraft(
                    "new-dependent",
                    "ivan",
                    "Новая вымышленная интерпретация.",
                    "interpretation",
                    tuple(original.sources),
                    0.2,
                ),
            )
            denied = await post(env, deletion)
            assert denied.status_code == 409 and denied.json()["code"] == "VERSION_CONFLICT"
            assert await env.ledger.entries(env.world.scope) == []
            expired_plan = await plan(env, "export")
            proof = await env.runtime.store.repository(
                MemoryPlanProof, PRINCIPALS[Owner.WORKSPACE], env.world.scope
            ).get(expired_plan["arguments"]["plan_id"])
            await put(
                env.runtime.store,
                env.runtime.registry,
                Owner.WORKSPACE,
                proof.scope,
                "expire-plan-fixture",
                proof.model_copy(
                    update={
                        "version": 2,
                        "updated_at": env.runtime.supervisor.clock.utc(),
                        "plan_expires_at": env.runtime.supervisor.clock.utc()
                        - timedelta(seconds=1),
                    }
                ),
                1,
            )
            assert (await post(env, expired_plan)).json()["code"] == "EXPIRED"
            export = await plan(env, "export")
            applied = await post(env, export)
            assert applied.status_code == 200
            await env.memory.set_rights(
                command(env.world.scope, "revoke-export"),
                env.source_id,
                1,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            download = "/api/v1/memory/exports/" + applied.json()["operation_id"] + "/content"
            assert (
                await env.client.get(download, params=scope_params(env.world.scope))
            ).status_code == 403
            assert (await post(env, preview_body(env, "export"))).status_code == 403
            # Restore rights only as explicit fixture administration, then prove STOP boundary.
            await env.memory.set_rights(
                command(env.world.scope, "fixture-rights"),
                env.source_id,
                2,
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
            )
            assert (await env.client.get(download, params=scope_params(env.world.scope))).json()[
                "code"
            ] == "STALE_CONTEXT"
            stored = await env.memory.get_source(env.world.scope, env.source_id)
            await put(
                env.runtime.store,
                env.runtime.registry,
                Owner.MEMORY,
                stored.scope,
                "expire-source-fixture",
                stored.model_copy(
                    update={
                        "version": 4,
                        "updated_at": env.runtime.supervisor.clock.utc(),
                        "retention": stored.retention.model_copy(
                            update={"expires_at": env.runtime.store.clock() - timedelta(seconds=1)}
                        ),
                    }
                ),
                3,
            )
            assert (
                await env.client.get(download, params=scope_params(env.world.scope))
            ).status_code == 403
            run = await start(env)
            active = preview_body(env, "delete")
            active["scope"] = run.scope.model_dump(mode="json")
            active["expected_version"] = 4
            response = await post(env, active)
            assert response.status_code == 429 and response.json()["code"] == "RESOURCE_BUSY"
            assert await env.ledger.entries(env.world.scope) == []
            assert not env.world.provider.calls

    asyncio.run(scenario())


def test_logout_during_preview_cannot_save_a_plan(tmp_path, monkeypatch):
    async def scenario():
        async with world(tmp_path) as env:
            entered, release = asyncio.Event(), asyncio.Event()
            original = env.runtime.retention.physical.dependents

            async def blocked(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(env.runtime.retention.physical, "dependents", blocked)
            pending = asyncio.create_task(post(env, preview_body(env, "delete")))
            await asyncio.wait_for(entered.wait(), 5)
            logged_out = await env.client.post("/api/v1/auth/logout", headers=env.headers, json={})
            assert logged_out.status_code == 204
            release.set()
            response = await pending
            assert response.status_code == 403
            proofs = await env.runtime.store.repository(
                MemoryPlanProof, PRINCIPALS[Owner.WORKSPACE], env.world.scope
            ).list()
            assert proofs == [] and await env.ledger.entries(env.world.scope) == []

    asyncio.run(scenario())
