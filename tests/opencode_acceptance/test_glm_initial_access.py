"""GLM OC-05: independent verification of the initial access decision on f809a6a.

Production init/build/ASGI, workplace files owner with the automatic 7-day ceiling,
synthetic model unused (no inference anywhere). Independent of the author's own
regressions in tests/workplace_access: different paths are taken on purpose.

- replay of the executed decision after the composition changes (the whole ceiling
  removed, and a member added later) returns the same durable receipt, grants no
  right and extends no term;
- a manifest version, a ready binding and the workspace policy changed between the
  preview pinning and the execute each refuse with 409 and write nothing stale;
- a write failure of one member inside the transaction rolls back the entire set
  (no permissions, the plan stays unexecuted), and a clean retry succeeds;
- a capability registered later (manifest, ready binding, ceiling entry) does not
  inherit the earlier decision, including on the replay path.
"""

import asyncio
from datetime import UTC, datetime

from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import CapabilityBinding, CapabilityManifest
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workplace_access import InitialAccessProof
from ai_agent_contracts.workspace import Workspace
from test_activity import WorkModel, prepared
from test_http import post, scope_params

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.permission_decisions import Ceiling
from ai_agent_core.m3_composition.state import PRINCIPALS, put

WEEK = 7 * 86400
SET = ["workplace.knowledge", "workplace.skills"]


def preview_command(host, key):
    return dict(
        type="workplace.access.preview",
        request_id=key,
        target="initial-access",
        scope=host.scope.model_dump(mode="json"),
        expected_version=1,
        arguments=dict(
            schema_id="workplace.access-args",
            data={"duration_seconds": WEEK, "constraints": ""},
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
    return {row.capability_id: row for row in rows if row.capability_id.startswith("workplace.")}


async def plan_of(host, key):
    planned = await post(host.env, preview_command(host, key))
    assert planned.status_code == 200, planned.text
    return planned.json()["resource_id"]


async def bump(rt, scope, owner, record, key):
    """Fixture setup: move one owner record to its next version through the owner."""
    updated = record.model_copy(
        update={"version": record.version + 1, "updated_at": rt.supervisor.clock.utc()}
    )
    await put(
        rt.store, rt.registry, owner, scope, key, updated, record.version, rt.readers
    )
    return updated


def test_initial_access_replay_stale_grounds_rollback_and_late_capability(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=7)
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            workspace_repo = rt.store.repository(
                Workspace, PRINCIPALS[Owner.WORKSPACE], scope
            )
            manifest_repo = rt.store.repository(
                CapabilityManifest, PRINCIPALS[Owner.WORKSPACE], scope
            )
            binding_repo = rt.store.repository(
                CapabilityBinding, PRINCIPALS[Owner.WORKSPACE], scope
            )

            # 1. One executed decision; the durable receipt is kept for the replays.
            plan_one = await plan_of(host, "glm-plan-1")
            first = await post(env, execute_command(host, "glm-grant-1", plan_one))
            assert first.status_code == 200, first.text
            granted = await rights(rt, scope)
            assert sorted(granted) == SET
            baseline = {
                key: (row.version, row.expires_at) for key, row in granted.items()
            }

            # 2. Replay after the whole ceiling is removed: same receipt, nothing new,
            #    no term extension (the original 09581ac failure mode).
            configured = rt.permission_ceiling
            rt.permission_ceiling = {}
            removed = await post(
                env, execute_command(host, "glm-grant-1", first.json()["resource_id"])
            )
            assert removed.status_code == 200, removed.text
            assert removed.json() == first.json()
            after = await rights(rt, scope)
            assert {k: (r.version, r.expires_at) for k, r in after.items()} == baseline
            rt.permission_ceiling = configured

            # 3. Stale grounds between the preview pinning and the execute.
            #    a) a manifest moved to a new version;
            stale = await plan_of(host, "glm-plan-manifest")
            manifests = {
                row.capability_id: row for row in await manifest_repo.list(limit=50)
            }
            await bump(
                rt, scope, Owner.WORKSPACE,
                manifests["workplace.skills"], "glm-manifest-bump",
            )
            conflict = await post(env, execute_command(host, "glm-exec-manifest", stale))
            assert conflict.status_code == 409
            unchanged = await rights(rt, scope)
            assert {k: (r.version, r.expires_at) for k, r in unchanged.items()} == baseline

            #    b) the workspace policy version moved;
            stale = await plan_of(host, "glm-plan-policy")
            workspace = await workspace_repo.get(scope.workspace_id)
            await bump(
                rt, scope, Owner.WORKSPACE,
                workspace.model_copy(
                    update={"policy_version": workspace.policy_version + 1}
                ),
                "glm-policy-bump",
            )
            conflict = await post(env, execute_command(host, "glm-exec-policy", stale))
            assert conflict.status_code == 409
            unchanged = await rights(rt, scope)
            assert {k: (r.version, r.expires_at) for k, r in unchanged.items()} == baseline

            #    c) the member's binding is no longer ready.
            stale = await plan_of(host, "glm-plan-binding")
            bindings = {
                row.capability_id: row for row in await binding_repo.list(limit=50)
            }
            await bump(
                rt, scope, Owner.WORKSPACE,
                bindings["workplace.skills"].model_copy(
                    update={"state": "disabled", "verified_at": None}
                ),
                "glm-binding-bump",
            )
            conflict = await post(env, execute_command(host, "glm-exec-binding", stale))
            assert conflict.status_code == 409
            unchanged = await rights(rt, scope)
            assert {k: (r.version, r.expires_at) for k, r in unchanged.items()} == baseline
            # restore a ready binding for the rollback scenario below
            disabled = {
                row.capability_id: row for row in await binding_repo.list(limit=50)
            }["workplace.skills"]
            await bump(
                rt, scope, Owner.WORKSPACE,
                disabled.model_copy(
                    update={"state": "ready", "verified_at": rt.supervisor.clock.utc()}
                ),
                "glm-binding-restore",
            )

            # 4. A write failure of one member rolls back the whole set. The fault is
            #    injected into the workspace transaction: the first member's Permission
            #    is written, the second one's put raises, and nothing may survive.
            plan = await plan_of(host, "glm-plan-rollback")
            original = rt.store.transaction

            class FailingPut:
                def __init__(self, inner):
                    self.inner = inner

                async def put(self, record, **kwargs):
                    if isinstance(record, Permission) and record.capability_id.endswith(
                        ".skills"
                    ):
                        raise ContractError(ErrorCode.VERSION_CONFLICT)
                    return await self.inner.put(record, **kwargs)

                def __getattr__(self, name):
                    return getattr(self.inner, name)

            def transaction(*args, **kwargs):
                inner = original(*args, **kwargs)

                class Wrapped:
                    async def __aenter__(self):
                        return FailingPut(await inner.__aenter__())

                    async def __aexit__(self, *exc):
                        return await inner.__aexit__(*exc)

                return Wrapped()

            monkeypatch.setattr(rt.store, "transaction", transaction)
            broken = await post(env, execute_command(host, "glm-exec-rollback", plan))
            assert broken.status_code == 409, broken.text
            monkeypatch.setattr(rt.store, "transaction", original)
            rolled = await rights(rt, scope)
            assert {k: (r.version, r.expires_at) for k, r in rolled.items()} == baseline
            proofs = await rt.store.repository(
                InitialAccessProof, PRINCIPALS[Owner.WORKSPACE], scope
            ).list(limit=10)
            assert all(p.executed_by is None for p in proofs if p.id == plan)
            retried = await post(env, execute_command(host, "glm-exec-rollback-2", plan))
            assert retried.status_code == 200, retried.text
            renewed = await rights(rt, scope)
            assert sorted(renewed) == SET and {r.version for r in renewed.values()} == {2}
            assert abs(
                (renewed["workplace.skills"].expires_at - datetime.now(UTC)).total_seconds()
                - WEEK
            ) < 120

            # 5. A capability registered later (manifest, ready binding, ceiling entry)
            #    does not inherit the decision — including via the old receipt replay.
            now = rt.supervisor.clock.utc()
            extra_manifest = CapabilityManifest(
                id="glm-extra-manifest",
                scope=scope,
                version=1,
                created_at=now,
                updated_at=now,
                capability_id="workplace.extra",
                capability_version=1,
                summary="Поздняя возможность",
                instructions="Только для проверки",
                input_schema_id="m3.empty",
                output_schema_id="m3.empty",
                effect="none",
                areas=[],
                network="none",
                runner_required=False,
            )
            extra_binding = CapabilityBinding(
                id="glm-extra-binding",
                binding_id="glm-extra-binding",
                scope=scope,
                version=1,
                created_at=now,
                updated_at=now,
                capability_id="workplace.extra",
                capability_version=1,
                adapter_id="synthetic",
                adapter_version="1",
                state="ready",
                verified_at=now,
            )
            for record in (extra_manifest, extra_binding):
                await put(
                    rt.store, rt.registry, Owner.WORKSPACE, scope,
                    "seed-" + record.id, record, None, rt.readers,
                )
            rt.permission_ceiling = {
                **rt.permission_ceiling, "workplace.extra": Ceiling("automatic", WEEK)
            }
            assert "workplace.extra" not in await rights(rt, scope)
            replay = await post(
                env, execute_command(host, "glm-grant-1", first.json()["resource_id"])
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == first.json()
            assert "workplace.extra" not in await rights(rt, scope)
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            assert "workplace.extra" in [m["capability_id"] for m in status["members"]]

            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count() == 0

    asyncio.run(scenario())
