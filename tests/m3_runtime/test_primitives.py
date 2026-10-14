"""Real owner storage around cancellation, clock transitions and reflection replay."""

import asyncio
from dataclasses import replace

import pytest
from ai_agent_application.development import Reflector
from ai_agent_application.gamemaster.clock import ActiveGMClock
from ai_agent_application.llm import Delta, Finished, LLMGateway, Started
from ai_agent_contracts.api import ControlArguments, ControlCommand, StopCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from dev_support import answer, idea
from dev_support import new_world as development_world
from llm_support import request, resolved
from operations_support import Fixture
from usage_support import command, make_scope
from usage_support import new_world as usage_world


def run(value):
    return asyncio.run(value)


def test_active_time_tracks_pause_resume_and_excludes_stopped_and_sleep_time():
    async def scenario():
        fixture = await Fixture().setup()
        owner = fixture.supervisor
        clock = ActiveGMClock(fixture.time.clock().elapsed)
        owner.observe(clock.observe)
        owner.set_gm(fixture.operator, False, expected_version=owner.snapshot.version)
        fixture.time.advance(20)
        assert clock.sample() == 20
        await owner.control(
            fixture.operator,
            ControlCommand(
                request_id="pause-clock",
                scope=owner.snapshot.scope,
                target="synthetic-agent",
                type="agent.pause",
                expected_version=owner.snapshot.version,
                arguments=ControlArguments(),
            ),
        )
        for _ in range(2):
            fixture.time.advance(10)
            owner.poll()
            clock.sample()
        assert clock.seconds == 20
        await owner.control(
            fixture.operator,
            ControlCommand(
                request_id="resume-clock",
                scope=owner.snapshot.scope,
                target="synthetic-agent",
                type="agent.resume",
                expected_version=owner.snapshot.version,
                arguments=ControlArguments(),
            ),
        )
        fixture.time.advance(20)
        assert clock.sample() == 40
        owner.stop(
            fixture.operator,
            StopCommand(
                request_id="stop-clock",
                scope=owner.snapshot.scope,
                target="synthetic-agent",
                type="agent.stop",
            ),
        )
        fixture.time.advance(1000)
        assert clock.sample() == 40

    run(scenario())


def test_attempt_is_durable_before_provider_and_terminal_receipt_is_saved(tmp_path):
    async def scenario():
        scope = make_scope("persona-a")
        world = await usage_world(__import__("pathlib").Path(tmp_path), [scope])
        binding = replace(resolved(), assignment_id="asg-a")

        class Journal:
            async def begin(self, report):
                await world.ledger.begin_attempt(command(scope), report)

            async def finish(self, report):
                await world.ledger.record_attempt(command(scope), report)

        class Provider:
            async def stream(self, call):
                row = await world.ledger.attempt(scope, "op-1.a1")
                assert row is not None and row.status == "running" and row.completed_at is None
                yield Started("mock")
                yield Delta("safe")
                yield Finished("stop")

        gateway = LLMGateway(lambda _: Provider(), lambda _: binding, journal=Journal())
        result = await gateway.generate(request())
        assert result.status == "completed"
        rows = await world.ledger.attempts(scope)
        assert len(rows) == 1 and rows[0].status == "completed"
        with pytest.raises(ContractError):
            await world.ledger.begin_attempt(command(scope), result.attempts[0])

    run(scenario())


def test_failed_before_call_journal_never_reaches_provider():
    async def scenario():
        calls = []

        class Journal:
            async def begin(self, report):
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)

            async def finish(self, report):
                raise AssertionError("there was no durable reservation")

        def provider(_):
            calls.append("IO")
            raise AssertionError("provider must not be called")

        result = await LLMGateway(provider, lambda _: resolved(), journal=Journal()).generate(
            request()
        )
        assert result.status == "cancelled" and calls == []

    run(scenario())


def test_reflection_keep_decision_survives_new_service_instance(tmp_path):
    async def scenario():
        world = await development_world(tmp_path, [answer(keep_state_reason="Keep", changes=[])])
        episode = await world.episode()
        first = await world.reflector.reflect(world.scope, "finished-session", [episode])
        again = Reflector(world.controller, world.reflector.generator, clock=world.clock)
        second = await again.reflect(world.scope, "finished-session", [episode])
        assert first.status == "kept" and second.status == "duplicate"
        assert len(world.provider.calls) == 1

    run(scenario())


def test_partial_reflection_replays_saved_model_result_without_second_call(tmp_path):
    async def scenario():
        world = await development_world(tmp_path, [[]])
        episode = await world.episode()
        world.provider.scripts[0] = answer(changes=[idea(episodes=[episode])])
        original = world.controller.submit

        async def crash(*args, **kwargs):
            raise RuntimeError("synthetic crash after model checkpoint")

        world.controller.submit = crash
        with pytest.raises(RuntimeError):
            await world.reflector.reflect(world.scope, "partial-session", [episode])
        world.controller.submit = original
        again = Reflector(world.controller, world.reflector.generator, clock=world.clock)
        report = await again.reflect(world.scope, "partial-session", [episode])
        assert report.status == "completed" and len(report.outcomes) == 1
        assert len(world.provider.calls) == 1

    run(scenario())


def test_reflection_interval_and_trigger_input_identity_survive_new_instance(tmp_path):
    async def scenario():
        world = await development_world(tmp_path, [answer(keep_state_reason="Keep", changes=[])])
        episode = await world.episode()
        assert (
            await world.reflector.reflect(world.scope, "session-one", [episode])
        ).status == "kept"
        rebuilt = Reflector(world.controller, world.reflector.generator, clock=world.clock)
        assert (await rebuilt.reflect(world.scope, "session-two", [episode])).status == "too_soon"
        with pytest.raises(ContractError) as failure:
            await rebuilt.reflect(world.scope, "session-one", ["different-episode"])
        assert failure.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
        assert len(world.provider.calls) == 1

    run(scenario())
