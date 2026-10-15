"""Real HTTP manual owner writes, immutable history and safe recovery after lost receipts."""

import pytest
from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Source
from ai_agent_contracts.panel import OwnerOperation
from ai_agent_contracts.persona import StateChange
from composition_support import run
from development_support import edit, prepared, rollback_execute, rollback_preview

from ai_agent_core.m3_composition.owner_operations import OwnerOperations
from ai_agent_core.m3_composition.state import PRINCIPALS, put


async def post(env, command):
    return await env.client.post("/api/v1/commands", json=command, headers=env.headers)


def test_accept_replay_and_conflict_preserve_one_state_change(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            command = edit(env)
            applied = await post(env, command)
            assert applied.status_code == 200, applied.text
            state = await env.state()
            assert state.values.data["values"]["openness"] == 0.6
            change = await env.current_change()
            assert (
                change.status.value == "APPLIED"
                and change.operator_reason == command["arguments"]["data"]["reason"]
            )
            repeated = await post(env, command)
            assert repeated.status_code == 200 and repeated.json() == applied.json()
            assert (await env.state()).version == state.version
            command["arguments"]["data"]["decision"] = "reject"
            conflict = await post(env, command)
            assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
            operations = await env.client.get("/api/v1/owner-operations", params=env.query)
            assert operations.status_code == 200, operations.text
            assert len(operations.json()["items"]) == 1
            assert operations.json()["items"][0]["state"] == "completed"
            assert not env.world.provider.calls

    run(scenario())


@pytest.mark.parametrize("correct", [False, True])
def test_reject_or_correct_records_reason_without_rewriting_state_or_evidence(tmp_path, correct):
    async def scenario():
        async with prepared(tmp_path) as env:
            before = await env.state()
            command = (
                edit(
                    env,
                    "change.interpretation",
                    interpretation="Достаточно одного уточнения.",
                    reason="Уточнение смысла опыта.",
                )
                if correct
                else edit(env, decision="reject", reason="Оснований недостаточно.")
            )
            response = await post(env, command)
            assert response.status_code == 200, response.text
            after = await env.current_change()
            assert after.status.value == ("AWAITING_APPROVAL" if correct else "REJECTED")
            assert after.operator_reason == command["arguments"]["data"]["reason"]
            assert after.evidence == env.change.evidence
            assert (await env.state()) == before
            if correct:
                assert after.interpretation == command["arguments"]["data"]["interpretation"]
            stale = await post(env, edit(env, request="stale"))
            assert stale.status_code == 409

    run(scenario())


async def applied_and_plan(env):
    accepted = await post(env, edit(env))
    assert accepted.status_code == 200, accepted.text
    preview = await post(env, rollback_preview(env, await env.current_change()))
    assert preview.status_code == 200, preview.text
    return preview.json()


def test_rollback_compensates_and_replay_survives_expired_proof(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            plan = await applied_and_plan(env)
            before = await env.state()
            command = rollback_execute(env, plan)
            response = await post(env, command)
            assert response.status_code == 200, response.text
            after = await env.state()
            assert (
                after.version == before.version + 1
                and after.values.data["values"]["openness"] == 0.5
            )
            compensation = await env.get(StateChange, response.json()["resource_id"])
            assert (
                compensation.revision_of == env.change.id
                and compensation.patch.data["kind"] == "rollback"
            )
            assert (await env.current_change()).status.value == "APPLIED"
            assert await env.world.store.repository(
                Source, PRINCIPALS[Owner.MEMORY], env.target
            ).get(env.source_ref.source_id)
            env.world.clock.advance(minutes=6)
            replay = await post(env, command)
            assert replay.status_code == 200 and replay.json() == response.json()
            assert (await env.state()).version == after.version

    run(scenario())


@pytest.mark.parametrize("changed", ["expired", "source", "state"])
def test_changed_preview_dependencies_block_rollback(tmp_path, changed):
    async def scenario():
        async with prepared(tmp_path) as env:
            plan = await applied_and_plan(env)
            before = await env.state()
            if changed == "expired":
                env.world.clock.advance(minutes=6)
            elif changed == "source":
                old = await env.world.store.repository(
                    Source, PRINCIPALS[Owner.MEMORY], env.target
                ).get(env.source_ref.source_id)
                revised = old.model_copy(
                    update=dict(version=old.version + 1, api_transfer_allowed=False)
                )
                await put(
                    env.world.store,
                    env.runtime.registry,
                    Owner.MEMORY,
                    env.target,
                    "revoke-source",
                    revised,
                    old.version,
                    env.readers,
                )
            else:
                revised = before.model_copy(
                    update=dict(version=before.version + 1, state_version=before.version + 1)
                )
                await put(
                    env.world.store,
                    env.runtime.registry,
                    Owner.PERSONA,
                    env.target,
                    "advance-state",
                    revised,
                    before.version,
                    env.readers,
                )
            response = await post(env, rollback_execute(env, plan))
            assert response.status_code in (403, 409), response.text
            assert (await env.state()).values == before.values

    run(scenario())


@pytest.mark.parametrize("rollback", [False, True])
def test_lost_completion_recovers_existing_owner_result_without_reapplying(
    tmp_path, monkeypatch, rollback
):
    async def scenario():
        async with prepared(tmp_path) as env:
            command = rollback_execute(env, await applied_and_plan(env)) if rollback else edit(env)
            save = OwnerOperations.save

            async def lose(self, row, previous):
                if row.state == "completed":
                    raise OSError("synthetic receipt loss")
                return await save(self, row, previous)

            monkeypatch.setattr(OwnerOperations, "save", lose)
            response = await post(env, command)
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "OUTCOME_UNKNOWN"
            before = await env.state()
            env.world.clock.advance(minutes=6)
            monkeypatch.setattr(OwnerOperations, "save", save)
            replay = await post(env, command)
            assert replay.status_code == 200, replay.text
            assert (await env.state()).version == before.version
            rows = await env.world.store.repository(
                OwnerOperation, PRINCIPALS[Owner.WORKSPACE], env.target
            ).list()
            assert all(row.state == "completed" for row in rows)

    run(scenario())


@pytest.mark.parametrize("race", ["epoch", "source", "rights"])
def test_late_epoch_source_or_operator_revocation_fences_owner_commit(tmp_path, monkeypatch, race):
    from dataclasses import replace

    from ai_agent_application.development import DevelopmentController
    from ai_agent_contracts.states import ErrorCode

    async def scenario():
        async with prepared(tmp_path) as env:
            before = await env.state()
            original = DevelopmentController.decide

            async def delayed(self, *args, **kwargs):
                if race == "epoch":
                    env.runtime.supervisor.critical_failure(ErrorCode.STALE_CONTEXT)
                elif race == "rights":
                    await env.world.store.provision(
                        env.target,
                        tuple(
                            replace(g, api_transfer=False) if g.principal.id == "operator" else g
                            for g in env.fixture_grants
                        ),
                    )
                else:
                    old = await env.world.store.repository(
                        Source, PRINCIPALS[Owner.MEMORY], env.target
                    ).get(env.source_ref.source_id)
                    revised = old.model_copy(
                        update=dict(version=old.version + 1, api_transfer_allowed=False)
                    )
                    await put(
                        env.world.store,
                        env.runtime.registry,
                        Owner.MEMORY,
                        env.target,
                        "late-revoke",
                        revised,
                        old.version,
                        env.readers,
                    )
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(DevelopmentController, "decide", delayed)
            response = await post(env, edit(env))
            assert response.status_code in (403, 409), response.text
            assert (await env.state()) == before
            assert (await env.current_change()).status.value == "AWAITING_APPROVAL"

    run(scenario())


def test_applied_interpretation_preserves_state_and_prior_revision(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            assert (await post(env, edit(env))).status_code == 200
            before = await env.state()
            old = await env.current_change()
            command = edit(
                env,
                "change.interpretation",
                request="annotation",
                interpretation="Одно полезное уточнение.",
                reason="Пересмотр смысла, не события.",
            )
            command["expected_version"] = old.version
            response = await post(env, command)
            assert response.status_code == 200, response.text
            current = await env.current_change()
            assert current.status == old.status and current.new_version == old.new_version
            assert current.evidence == old.evidence and current.interpretation != old.interpretation
            prior = await env.world.store.repository(
                StateChange, PRINCIPALS[Owner.PERSONA], env.target
            ).get(old.id, version=old.version)
            assert prior == old and (await env.state()) == before

    run(scenario())


def test_gm_numeric_configuration_cancel_and_pause_use_resource_versions(tmp_path):
    from datetime import timedelta

    from ai_agent_contracts.persona import Opportunity, StoryArc
    from ai_agent_contracts.states import OpportunityState

    async def scenario():
        async with prepared(tmp_path) as env:
            now = env.world.clock()
            common = dict(scope=env.target, version=1, created_at=now, updated_at=now)
            arc = StoryArc(
                id="manual-arc",
                grounds=[env.source_ref],
                episode_ids=[],
                direction="Вымышленное исследование.",
                boundaries="Без принуждения.",
                status="open",
                **common,
            )
            opportunities = [
                Opportunity(
                    id="manual-offer-" + str(i),
                    arc_id=arc.id,
                    grounds=[env.source_ref],
                    applicability="Вымышленная возможность.",
                    boundaries="Без принуждения.",
                    expires_at=now + timedelta(hours=1),
                    capability_ids=[],
                    status=OpportunityState.OFFERED,
                    **common,
                )
                for i in range(2)
            ]
            for row in [arc, *opportunities]:
                await put(
                    env.world.store,
                    env.runtime.registry,
                    Owner.GM,
                    env.target,
                    "seed-" + row.id,
                    row,
                    None,
                    env.readers,
                )
            gm = await env.runtime.state.gm(env.target)
            command = edit(env, "gm.configure", request="intensity", intensity=0.8)
            command.update(target="gm-runtime", expected_version=gm.version)
            result = await post(env, command)
            assert result.status_code == 200, result.text
            assert (await env.runtime.state.gm(env.target)).intensity == 0.8
            assert (await post(env, command)).json() == result.json()
            for kind, target in [("opportunity.cancel", opportunities[0]), ("arc.pause", arc)]:
                command = edit(env, kind, request=kind)
                command.update(target=target.id, expected_version=target.version)
                command["arguments"]["data"] = {}
                result = await post(env, command)
                assert result.status_code == 200, result.text
                assert (await post(env, command)).json() == result.json()
            repo = env.world.store.repository(Opportunity, PRINCIPALS[Owner.GM], env.target)
            assert all(
                row.status == OpportunityState.CANCELLED
                for row in [await repo.get(o.id) for o in opportunities]
            )
            assert (
                await env.world.store.repository(StoryArc, PRINCIPALS[Owner.GM], env.target).get(
                    arc.id
                )
            ).status == "paused"
            settings = await env.client.get("/api/v1/gm-settings", params=env.query)
            assert settings.json()["items"][0]["intensity"] == 0.8

    run(scenario())
