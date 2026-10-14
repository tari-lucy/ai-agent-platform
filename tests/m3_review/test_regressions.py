"""Independent review repros: accepted code must retain safety across awaited work."""

import pytest
from ai_agent_application.gamemaster import Classification
from ai_agent_application.laboratory import Scenario
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.persona import Opportunity, StoryArc
from ai_agent_contracts.states import ChangeState, ErrorCode
from dev_support import answer, idea
from dev_support import new_world as development_world
from extraction_support import new_world as extraction_world
from gm_support import GM, run
from gm_support import new_world as gm_world
from lab_support import new_env


def test_gm_stop_during_classifier_cannot_create_an_arc(tmp_path):
    async def scenario():
        world = await gm_world(tmp_path)
        ground = await world.source()

        class StopDuringScreen:
            async def classify(self, text, rules):
                world.state["admitted"] = False
                return Classification()

        world.gm.classifier = StopDuringScreen()
        try:
            await world.gm.open_arc(world.scope, "late", [ground], "A quiet discovery", "Safe")
        except ContractError as error:
            assert error.error.code is ErrorCode.STALE_CONTEXT
        return await world.store.repository(StoryArc, GM, world.scope).list()

    assert run(scenario()) == []


def test_pausing_arc_cancels_waiting_offer_and_prevents_accept(tmp_path):
    async def scenario():
        world = await gm_world(tmp_path)
        arc, ground = await world.arc()
        offered = await world.gm.propose(
            world.scope, arc.arc.id, "find", "tracks", "Tracks", "Safe", [ground]
        )
        await world.gm.set_arc_status(world.scope, arc.arc.id, "paused")
        with pytest.raises(ContractError):
            await world.gm.accept(world.scope, offered.opportunity.id)
        return await world.gm.character_view(world.scope)

    assert run(scenario()) == []


def test_expired_opportunity_can_be_finalized_by_owner_without_public_recall(tmp_path):
    from datetime import timedelta

    async def scenario():
        world = await gm_world(tmp_path)
        arc, ground = await world.arc()
        offered = await world.gm.propose(
            world.scope,
            arc.arc.id,
            "find",
            "tracks",
            "Tracks",
            "Safe",
            [ground],
            ttl=timedelta(minutes=1),
        )
        world.clock.advance(minutes=2)
        expired = await world.gm.expire_due(world.scope)
        ordinary = await world.store.repository(Opportunity, GM, world.scope).get(
            offered.opportunity.id
        )
        return expired, ordinary, offered.opportunity.id

    expired, ordinary, identifier = run(scenario())
    assert expired == [identifier]
    assert ordinary is None


def test_development_stop_during_context_read_does_not_apply(tmp_path):
    async def scenario():
        world = await development_world(tmp_path)
        episode = await world.episode()
        outcome = await world.controller.submit(
            world.scope, await world.candidate(episodes=[episode])
        )
        original = world.controller._context

        async def stop_during_context(scope):
            result = await original(scope)
            world.control.admitted = False
            return result

        world.controller._context = stop_during_context
        with pytest.raises(ContractError) as rejected:
            await world.controller.apply(world.scope, outcome.change.id)
        assert rejected.value.error.code is ErrorCode.STALE_CONTEXT
        return await world.state(), await world.controller.history(world.scope)

    state, history = run(scenario())
    assert state.version == 1
    assert history[0].status is ChangeState.READY


def test_reflection_result_after_stop_never_creates_proposals(tmp_path):
    async def scenario():
        world = await development_world(tmp_path, [[]])
        episode = await world.episode()
        world.provider.scripts[0] = answer(changes=[idea(episodes=[episode])])
        original = world.reflector.generator.generate

        async def stop_after_model(*args, **kwargs):
            result = await original(*args, **kwargs)
            world.control.admitted = False
            return result

        world.reflector.generator.generate = stop_after_model
        report = await world.reflector.reflect(world.scope, "late-result", [episode])
        return report, await world.controller.history(world.scope)

    report, history = run(scenario())
    assert report.status in ("deferred", "cancelled")
    assert history == []


def test_report_detects_secret_and_does_not_return_the_secret(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)

        async def unsafe_log(lab):
            lab.note("bad-input", text="api_key = sk-abcdefghijklmnop1234")

        return await env.runner.run(Scenario("unsafe-log", 1, "Synthetic", unsafe_log))

    report = run(scenario())
    assert report.status == "failed"
    assert "NO_SECRETS_IN_REPORT" in [finding.name for finding in report.failed]
    assert "sk-abcdefghijklmnop1234" not in report.to_json()


def test_unconfigured_extraction_does_not_enable_autonomy(tmp_path):
    from ai_agent_application.extraction import Extractor

    async def scenario():
        world = await extraction_world(tmp_path, [[]])
        ref = await world.source("A synthetic source")
        extractor = Extractor(world.memory, world.extractor.generator)
        report = await extractor.extract(world.scope, [ref.source_id])
        return report, world.provider.calls

    report, calls = run(scenario())
    assert report.status == "not_admitted"
    assert calls == []
