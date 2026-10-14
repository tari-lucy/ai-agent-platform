"""Review checks for rights, replay and applying exactly the reviewed revision."""

import asyncio

import pytest
from ai_agent_application.gamemaster import StoryRule, StoryRules
from ai_agent_application.laboratory import VirtualClock
from ai_agent_application.memory import ClaimDraft, EpisodeDraft
from ai_agent_application.persona import import_markdown
from ai_agent_application.social import AspectLimit, SocialPolicy
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from extraction_support import answer
from extraction_support import new_world as extraction_world
from gm_support import draft
from gm_support import new_world as gm_world
from lab_support import apply_command
from social_support import command, make_scope
from social_support import new_world as social_world
from tests.laboratory.test_transfer import EDITED
from tests.laboratory.test_transfer import world as transfer_world


def run(value):
    return asyncio.run(value)


def test_private_claim_is_not_added_to_an_allowed_source_model_context(tmp_path):
    async def scenario():
        world = await extraction_world(tmp_path, [answer()])
        private = await world.source("Private synthetic experience", api=False)
        await world.memory.record_claim(
            world.command(),
            ClaimDraft(
                claim_id="private-claim",
                subject_id="person-nika",
                content="SYNTHETIC_PRIVATE_CLAIM",
                knowledge_type="reported",
                sources=(private,),
                confidence=0.5,
            ),
        )
        public = await world.source("Allowed synthetic experience")
        await world.extractor.extract(world.scope, [public.source_id])
        assert "SYNTHETIC_PRIVATE_CLAIM" not in world.prompt()
        assert "Allowed synthetic experience" in world.prompt()

    run(scenario())


def test_relationship_rate_and_spent_experience_survive_service_restart(tmp_path):
    async def scenario():
        scope = make_scope()
        policy = SocialPolicy(aspect_limits=(AspectLimit("trust", 0, 1, 0.05, 0.05),))
        world = await social_world(tmp_path, [scope], policy=policy)
        world.social.admitted = lambda: True
        person, ref = await world.person(scope)
        await world.memory.record_episode(
            command(scope),
            EpisodeDraft(
                sources=(ref,),
                participant_ids=(person.id,),
                choice="Met",
                actual_outcome="Talked",
                operation_id="meeting",
                provenance="observation",
            ),
        )
        await world.social.ensure_relationship(command(scope), person.id, {"trust": 0.5})
        with pytest.raises(ContractError):
            await world.social.apply_aspects(
                command(scope), person.id, 1, {"trust": 0.99}, [ref], "jump"
            )
        await world.social.apply_aspects(
            command(scope), person.id, 1, {"trust": 0.55}, [ref], "first"
        )
        restarted = world.restarted()
        restarted.admitted = lambda: True
        with pytest.raises(ContractError):
            await restarted.apply_aspects(
                command(scope), person.id, 2, {"trust": 0.6}, [ref], "reuse"
            )
        with pytest.raises(ContractError) as conflict:
            await restarted.apply_aspects(
                command(scope), person.id, 1, {"trust": 0.54}, [ref], "first"
            )
        assert conflict.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
        assert (await restarted.get_relationship(scope, person.id)).version == 2

    run(scenario())


def test_gm_rolls_are_persisted_and_new_rules_recheck_old_offer(tmp_path):
    async def scenario():
        world = await gm_world(tmp_path, [draft()])
        arc, ref = await world.arc()
        made = await world.gm.suggest(
            world.scope, arc.arc.id, [ref], ["explore", "talk"], world.generator
        )
        saved = await world.gm.storage.repository(
            type(made.opportunity), world.gm.principal, world.scope
        ).get(made.opportunity.id)
        assert saved.rolls[0].chosen == made.roll[0].chosen
        assert saved.family_id and saved.rules_version == world.gm.rules.version
        world.gm.rules = StoryRules(
            2, (StoryRule("new-ban", "project", "hard", "Forbidden", ("карта",)),)
        )
        with pytest.raises(ContractError):
            await world.gm.accept(world.scope, saved.id)

    run(scenario())


def test_transfer_does_not_apply_a_source_revision_changed_after_preview_recheck(tmp_path):
    async def scenario():
        env, persona, test, transfer, settings = await transfer_world(tmp_path)
        preview = await transfer.preview(test, env.work, ["gm_intensity", "profile"])
        original = persona.export_markdown
        calls = 0

        async def change_before_apply(scope):
            nonlocal calls
            if scope == test:
                calls += 1
                if calls == 2:
                    markdown = EDITED.replace('"humor": 0.7', '"humor": 0.8')
                    parsed, _ = import_markdown(markdown)
                    current = await persona.get_profile(test)
                    await persona.apply(
                        apply_command(test, markdown, parsed.digest, current.version, "race")
                    )
            return await original(scope)

        persona.export_markdown = change_before_apply
        with pytest.raises(ContractError) as error:
            await transfer.transfer(
                test, env.work, ["gm_intensity", "profile"], "d", preview.digest
            )
        assert error.value.error.code is ErrorCode.STALE_CONTEXT
        assert (await persona.get_profile(env.work)).version == 1
        assert settings.get(env.work, "gm_intensity") == 1.0

    run(scenario())


def test_virtual_clock_bounds_self_rescheduling_callbacks():
    async def scenario():
        clock = VirtualClock()

        async def repeat():
            clock.at(clock.now, repeat)

        clock.at(clock.now, repeat)
        with pytest.raises(ValueError, match="CLOCK_CALLBACK_BUDGET"):
            await clock.advance(seconds=1)

    run(scenario())


def test_resolving_after_partial_failure_cannot_change_already_recorded_facts(tmp_path):
    async def scenario():
        world = await gm_world(tmp_path)
        arc, ref = await world.arc()
        made = await world.gm.propose(
            world.scope, arc.arc.id, "find", "tracks", "Tracks", "Safe", [ref]
        )
        await world.gm.accept(world.scope, made.opportunity.id)
        original = world.gm._move

        async def crash(*args, **kwargs):
            raise RuntimeError("synthetic crash after memory commit")

        world.gm._move = crash
        with pytest.raises(RuntimeError):
            await world.gm.resolve(world.scope, made.opportunity.id, "Looked", "Found stone", ref)
        world.gm._move = original
        with pytest.raises(ContractError) as mismatch:
            await world.gm.resolve(world.scope, made.opportunity.id, "Left", "Found nothing", ref)
        assert mismatch.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
        resolved = await world.gm.resolve(
            world.scope, made.opportunity.id, "Looked", "Found stone", ref
        )
        assert resolved.actual_outcome == "Found stone"

    run(scenario())


def test_development_replay_cannot_replace_the_evidence_or_interpretation(tmp_path):
    from dataclasses import replace

    from dev_support import new_world as development_world

    async def scenario():
        world = await development_world(tmp_path)
        episode = await world.episode()
        candidate = await world.candidate(episodes=[episode])
        await world.controller.submit(world.scope, candidate)
        with pytest.raises(ContractError) as mismatch:
            await world.controller.submit(
                world.scope, replace(candidate, interpretation="A different explanation")
            )
        assert mismatch.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT

    run(scenario())
