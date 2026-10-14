"""Explicit transfer of selected settings from test to work (M3-04)."""

import json

import pytest
from ai_agent_application.laboratory import (
    NEVER_TRANSFERRED,
    REFERENCE_PROFILE,
    MemorySettings,
    SettingsTransfer,
)
from ai_agent_application.laboratory.lab import MEMORY, PERSONA, SOCIAL
from ai_agent_application.persona import import_markdown
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Person, Source
from ai_agent_contracts.persona import CharacterState, Profile
from ai_agent_contracts.states import ErrorCode
from lab_support import (
    apply_command,
    create_command,
    new_env,
    persona_service,
    run,
    scope_of,
)

EDITED = REFERENCE_PROFILE.replace('"humor": 0.6', '"humor": 0.7').replace(
    "Лабораторная Искра", "Лабораторная Искра v2"
)


async def world(tmp_path, *, edited=True, test_profile=REFERENCE_PROFILE):
    env = await new_env(tmp_path)
    persona = await persona_service(env)
    test = await env.runner.scope_factory("t")
    await persona.create(create_command(test, test_profile, "create-test"))
    await persona.create(create_command(env.work, REFERENCE_PROFILE, "create-work"))
    if edited:
        parsed, _ = import_markdown(EDITED)
        profile = await persona.get_profile(test)
        await persona.apply(
            apply_command(test, EDITED, parsed.digest, profile.version, "edit-test")
        )
    settings = MemorySettings()
    settings.set(test, "gm_intensity", 0.5)
    settings.set(env.work, "gm_intensity", 1.0)
    settings.set(test, "story_rules_version", 7)
    settings.set(env.work, "story_rules_version", 3)
    return env, persona, test, SettingsTransfer(persona, settings), settings


def test_preview_shows_the_diff_and_what_never_moves(tmp_path):
    async def scenario():
        env, persona, test, transfer, _ = await world(tmp_path)
        return await transfer.preview(test, env.work, ["profile", "gm_intensity"])

    preview = run(scenario())
    profile, intensity = preview.items
    assert profile.key == "profile" and profile.before != profile.after
    assert "начальное значение: humor" in profile.changes and not preview.blocked
    assert intensity.before == 1.0 and intensity.after == 0.5
    assert preview.never_transferred == NEVER_TRANSFERRED
    assert {"episodes", "people", "character_state", "sources"} <= set(preview.never_transferred)


def test_only_selected_settings_move_after_an_operator_decision_with_the_previewed_digest(tmp_path):
    async def scenario():
        env, persona, test, transfer, settings = await world(tmp_path)
        preview = await transfer.preview(test, env.work, ["profile"])
        with pytest.raises(ContractError) as no_decision:
            await transfer.transfer(test, env.work, ["profile"], "", preview.digest)
        with pytest.raises(ContractError) as wrong_digest:
            await transfer.transfer(test, env.work, ["profile"], "decision-1", "0" * 64)
        untouched = await persona.get_profile(env.work)
        result = await transfer.transfer(test, env.work, ["profile"], "decision-1", preview.digest)
        moved = await persona.get_profile(env.work)
        source_profile = await persona.get_profile(test)
        return (env, no_decision, wrong_digest, untouched, result, moved, source_profile, settings)

    env, no_decision, wrong_digest, untouched, result, moved, source_profile, settings = run(
        scenario()
    )
    assert no_decision.value.error.code is ErrorCode.STALE_CONTEXT
    assert wrong_digest.value.error.code is ErrorCode.STALE_CONTEXT
    assert untouched.version == 1 and result.applied == ("profile",)
    assert moved.accepted_revision_id == source_profile.accepted_revision_id and moved.version == 2
    assert settings.get(env.work, "gm_intensity") == 1.0  # not selected, so not moved


def test_plain_settings_transfer_individually(tmp_path):
    async def scenario():
        env, _, test, transfer, settings = await world(tmp_path)
        preview = await transfer.preview(test, env.work, ["gm_intensity"])
        await transfer.transfer(test, env.work, ["gm_intensity"], "d", preview.digest)
        return env, settings

    env, settings = run(scenario())
    assert settings.get(env.work, "gm_intensity") == 0.5
    assert settings.get(env.work, "story_rules_version") == 3


def drop_property(markdown: str, name: str) -> str:
    """The reference profile without one property (properties, values and bounds)."""
    parts = markdown.split("```json")
    out = [parts[0]]
    for part in parts[1:]:
        body, rest = part.split("```", 1)
        data = json.loads(body)
        if "properties" in data:
            data["properties"] = [p for p in data["properties"] if p["name"] != name]
        if "values" in data:
            data["values"].pop(name, None)
        if "bounds" in data:
            data["bounds"] = [b for b in data["bounds"] if b["name"] != name]
        out.append("```json\n" + json.dumps(data, ensure_ascii=False) + "\n```" + rest)
    return "".join(out)


def test_a_test_biography_never_becomes_a_work_biography(tmp_path):
    async def scenario():
        from ai_agent_application.memory import MemoryService, SourceDraft
        from ai_agent_contracts.api import MessageArguments, MessageCommand
        from ai_agent_contracts.states import Provenance

        env, persona, test, transfer, _ = await world(tmp_path)
        memory = MemoryService(env.store, MEMORY, persona.extensions, clock=env.runner.clock)
        await memory.register_source(
            MessageCommand(
                request_id="lived-in-test",
                scope=test,
                target="t",
                type="dialogue.message",
                arguments=MessageArguments(text="x"),
            ),
            SourceDraft(
                provenance=Provenance.SIMULATION,
                source_type="lab-event",
                adapter_id="lab",
                adapter_version="1",
                occurred_at=env.runner.clock.now,
                content="Опыт только из теста.",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
                external_id="t-1",
            ),
        )
        before = await env.store.repository(Source, MEMORY, env.work).list(limit=50)
        preview = await transfer.preview(test, env.work, ["profile"])
        await transfer.transfer(test, env.work, ["profile"], "d", preview.digest)
        after = await env.store.repository(Source, MEMORY, env.work).list(limit=50)
        people = await env.store.repository(Person, SOCIAL, env.work).list(limit=50)
        state = await env.store.repository(CharacterState, PERSONA, env.work).get(
            "state:" + env.work.persona_id
        )
        return before, after, people, state

    before, after, people, state = run(scenario())
    assert [r.id for r in before] == [r.id for r in after] and people == []
    assert all("только из теста" not in (r.content or "") for r in after)
    assert state.values.data["values"]["humor"] == 0.6  # live state keeps its accumulated values


def test_incompatible_revisions_are_blocked_before_anything_changes(tmp_path):
    async def scenario():
        slim = drop_property(REFERENCE_PROFILE, "openness")
        env, persona, test, transfer, _ = await world(tmp_path, edited=False, test_profile=slim)
        preview = await transfer.preview(test, env.work, ["profile"])
        with pytest.raises(ContractError) as info:
            await transfer.transfer(test, env.work, ["profile"], "d", preview.digest)
        return preview, info.value.error.code, await persona.get_profile(env.work)

    preview, code, work_profile = run(scenario())
    assert preview.blocked and preview.items[0].blockers == ("STATE_MIGRATION_REQUIRED:openness",)
    assert code is ErrorCode.STALE_CONTEXT and work_profile.version == 1


def test_directions_keys_and_scopes_are_validated(tmp_path):
    async def scenario():
        env, _, test, transfer, _ = await world(tmp_path)
        errors = []
        for source, target, keys in (
            (env.work, test, ["profile"]),  # wrong direction
            (test, test, ["profile"]),  # not a work target
            (test, env.work, []),  # nothing selected
            (test, env.work, ["memory"]),  # not a transferable setting
            (test, env.work, ["profile", "profile"]),  # duplicate
            (
                test,
                scope_of("work", "space-w2").model_copy(update={"workspace_id": "other"}),
                ["profile"],
            ),
        ):
            with pytest.raises(ContractError) as info:
                await transfer.preview(source, target, keys)
            errors.append(info.value.error.code)
        return errors

    assert set(run(scenario())) == {ErrorCode.INVALID_ARGUMENTS}


def test_creating_the_work_profile_when_none_exists_yet(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)
        persona = await persona_service(env)
        test = await env.runner.scope_factory("t")
        await persona.create(create_command(test, REFERENCE_PROFILE, "create-test"))
        transfer = SettingsTransfer(persona, MemorySettings())
        fresh_work = scope_of("work", "space-work", persona="persona-new")
        await env.store.provision(
            fresh_work,
            __import__("ai_agent_application.laboratory", fromlist=["lab_grants"]).lab_grants(),
        )
        test_other = test.model_copy(update={"persona_id": "persona-new"})
        await env.store.provision(
            test_other,
            __import__("ai_agent_application.laboratory", fromlist=["lab_grants"]).lab_grants(),
        )
        await persona.create(create_command(test_other, REFERENCE_PROFILE, "create-test-2"))
        preview = await transfer.preview(test_other, fresh_work, ["profile"])
        await transfer.transfer(test_other, fresh_work, ["profile"], "d", preview.digest)
        return (
            preview,
            await persona.get_profile(fresh_work),
            await env.store.repository(Profile, PERSONA, fresh_work).get("persona-new"),
        )

    preview, created, same = run(scenario())
    assert preview.items[0].before is None and "создаётся" in preview.items[0].changes[0]
    assert created is not None and created.version == 1 and same.id == created.id
