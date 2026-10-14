"""Actual ASGI/SQLite preview, exact review binding, current ACL and partial reconciliation."""

import pytest
from ai_agent_application.storage import Owner
from ai_agent_contracts.panel import SettingsTransferPreview
from ai_agent_contracts.persona import CharacterState, Profile
from composition_support import run
from transfer_support import apply_command, prepared

from ai_agent_core.m3_composition.settings import DurableSettings
from ai_agent_core.m3_composition.state import PRINCIPALS, put

PREVIEW = "/api/v1/laboratory/transfer-preview"


async def preview(env, keys=("gm_intensity",)):
    response = await env.client.post(PREVIEW, json=env.command(keys), headers=env.headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_apply_status_and_successful_replay_do_not_repeat_writes(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            plan = await preview(env, ("profile", "gm_intensity", "story_rules_version"))
            assert not plan["blocked"]
            assert "Вымышленный тестовый канон." in plan["items"][0]["after"]
            assert plan["items"][2]["after"]["rules_version"] == 2
            stored = await env.world.store.repository(
                SettingsTransferPreview, PRINCIPALS[Owner.WORKSPACE], env.target
            ).get(plan["id"])
            assert all(i.before is None and i.after is None for i in stored.items)
            before = await env.world.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], env.target
            ).get("state:" + env.target.persona_id)
            applied = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert applied.status_code == 200, applied.text
            profile = await env.world.store.repository(
                Profile, PRINCIPALS[Owner.PERSONA], env.target
            ).get(env.target.persona_id)
            assert profile.canon == "Вымышленный тестовый канон."
            after = await env.world.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], env.target
            ).get(before.id)
            assert after.values == before.values
            gm = await env.runtime.state.gm(env.target)
            assert gm.intensity == 0.9 and gm.rules_version == 2
            replay = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert replay.status_code == 200 and replay.json() == applied.json()
            assert (await env.runtime.state.gm(env.target)).version == gm.version
            status = await env.client.get(
                "/api/v1/settings-transfer-status/transfer-1", params=env.query
            )
            assert status.status_code == 200 and status.json()["record"]["status"] == "completed"
            assert (
                len(
                    (await env.client.get("/api/v1/settings-transfers", params=env.query)).json()[
                        "items"
                    ]
                )
                == 1
            )
            assert not env.world.provider.calls

    run(scenario())


@pytest.mark.parametrize("change", ["digest", "keys", "expired", "source", "permission"])
def test_review_or_rights_change_closes_apply_without_mutating_settings(tmp_path, change):
    async def scenario():
        async with prepared(tmp_path) as env:
            plan = await preview(env)
            command = apply_command(env, plan)
            if change == "digest":
                command["arguments"]["data"]["preview_digest"] = "0" * 64
            elif change == "keys":
                command["arguments"]["data"]["keys"] = ["gm_intensity", "profile"]
            elif change == "expired":
                env.world.clock.advance(minutes=6)
            elif change == "permission":
                from dataclasses import replace

                await env.world.store.provision(
                    env.source,
                    tuple(
                        replace(g, api_transfer=False) if g.principal.id == "operator" else g
                        for g in env.fixture_grants
                    ),
                )
            else:
                old = await env.runtime.state.gm(env.source)
                await put(
                    env.world.store,
                    env.runtime.registry,
                    Owner.GM,
                    env.source,
                    "changed-source",
                    old.model_copy(update={"version": old.version + 1, "intensity": 0.7}),
                    old.version,
                    env.readers,
                )
            result = await env.client.post("/api/v1/commands", json=command, headers=env.headers)
            assert result.status_code in (403, 409), result.text
            assert (await env.runtime.state.gm(env.target)).intensity == 0.5

    run(scenario())


def test_private_source_is_not_disclosed_by_preview(tmp_path):
    async def scenario():
        async with prepared(tmp_path, source_api=False) as env:
            result = await env.client.post(
                PREVIEW, json=env.command(("profile",)), headers=env.headers
            )
            assert result.status_code == 403 and "Вымышленный тестовый канон." not in result.text

    run(scenario())


def test_partial_transfer_is_visible_and_only_explicit_review_unblocks_it(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path) as env:
            plan = await preview(env, ("gm_intensity", "story_rules_version"))
            original = DurableSettings.set

            async def fail(self, scope, key, value):
                if key == "story_rules_version":
                    raise RuntimeError("synthetic owner failure")
                return await original(self, scope, key, value)

            monkeypatch.setattr(DurableSettings, "set", fail)
            result = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert result.status_code == 503
            status = (
                await env.client.get(
                    "/api/v1/settings-transfer-status/transfer-1", params=env.query
                )
            ).json()["record"]
            assert status["status"] == "reconciliation_required" and status["applied_keys"] == [
                "gm_intensity"
            ]
            repeated = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert repeated.status_code == 409 and repeated.json()["code"] == "OUTCOME_UNKNOWN"
            ack = env.command(("gm_intensity", "story_rules_version"), request="acknowledge")
            ack["expected_version"] = status["version"]
            ack["arguments"]["data"].update(
                action="acknowledge", preview_digest=plan["preview_digest"]
            )
            assert (
                await env.client.post("/api/v1/commands", json=ack, headers=env.headers)
            ).status_code == 200
            assert not await env.runtime.policy.transfers.pending(env.target)
            assert (await env.runtime.state.gm(env.target)).intensity == 0.9

    run(scenario())


def test_revocation_inside_owner_write_is_checked_in_the_same_transaction(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path) as env:
            plan = await preview(env)
            original = DurableSettings.set

            async def revoke_then_write(self, scope, key, value):
                from dataclasses import replace

                await env.world.store.provision(
                    env.source,
                    tuple(
                        replace(g, api_transfer=False) if g.principal.id == "operator" else g
                        for g in env.fixture_grants
                    ),
                )
                return await original(self, scope, key, value)

            monkeypatch.setattr(DurableSettings, "set", revoke_then_write)
            result = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert result.status_code == 403
            assert (await env.runtime.state.gm(env.target)).intensity == 0.5
            assert await env.runtime.policy.transfers.pending(env.target)

    run(scenario())


def test_rule_downgrade_is_blocked_before_other_selected_settings_move(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            old = await env.runtime.state.gm(env.source)
            await put(
                env.world.store,
                env.runtime.registry,
                Owner.GM,
                env.source,
                "old-rules",
                old.model_copy(update={"version": old.version + 1, "rules_version": 1}),
                old.version,
                env.readers,
            )
            old = await env.runtime.state.gm(env.target)
            await put(
                env.world.store,
                env.runtime.registry,
                Owner.GM,
                env.target,
                "new-rules",
                old.model_copy(update={"version": old.version + 1, "rules_version": 2}),
                old.version,
                env.readers,
            )
            command = env.command(("gm_intensity", "story_rules_version"))
            command["expected_version"] = 3
            result = await env.client.post(PREVIEW, json=command, headers=env.headers)
            assert result.status_code == 200
            plan = result.json()
            assert plan["blocked"] and "RULES_VERSION_CONFLICT" in plan["items"][1]["blockers"]
            result = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert result.status_code == 409
            assert (await env.runtime.state.gm(env.target)).intensity == 0.5

    run(scenario())


def test_work_character_state_change_at_profile_owner_entry_invalidates_review(
    tmp_path, monkeypatch
):
    async def scenario():
        async with prepared(tmp_path) as env:
            from ai_agent_application.persona import PersonaService

            plan = await preview(env, ("profile",))
            original = PersonaService.apply

            async def change_state_then_apply(self, command):
                old = await self.get_state(env.target)
                await put(
                    env.world.store,
                    env.runtime.registry,
                    Owner.PERSONA,
                    env.target,
                    "concurrent-state-change",
                    old.model_copy(
                        update={"version": old.version + 1, "state_version": old.state_version + 1}
                    ),
                    old.version,
                    env.readers,
                )
                return await original(self, command)

            monkeypatch.setattr(PersonaService, "apply", change_state_then_apply)
            result = await env.client.post(
                "/api/v1/commands", json=apply_command(env, plan), headers=env.headers
            )
            assert result.status_code == 403
            current = await env.world.store.repository(
                Profile, PRINCIPALS[Owner.PERSONA], env.target
            ).get(env.target.persona_id)
            assert current.canon != "Вымышленный тестовый канон."

    run(scenario())
