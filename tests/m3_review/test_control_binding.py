"""M3 callbacks must not regain authority when a later run becomes active."""

from asyncio import run

import pytest
from ai_agent_contracts.api import ControlArguments, ControlCommand, StopCommand
from operations_support import Fixture

from ai_agent_core.m3_control import PinnedM3Control


def test_stop_invalidates_the_pinned_job_even_when_supervisor_recovers():
    async def scenario():
        fixture = await Fixture().setup()
        supervisor = fixture.supervisor
        permit = supervisor.admit("reflection")
        binding = PinnedM3Control(supervisor, permit, "reflection")
        assert binding.admitted()
        supervisor.stop(
            fixture.operator,
            StopCommand(
                request_id="stop-m3",
                scope=supervisor.snapshot.scope,
                target="synthetic-agent",
                type="agent.stop",
            ),
        )
        assert not binding.admitted()
        assert not binding.snapshot().admitted
        await supervisor.flush()
        await supervisor.control(
            fixture.operator,
            ControlCommand(
                request_id="restart-m3",
                type="agent.start",
                target="synthetic-agent",
                scope=supervisor.snapshot.scope,
                expected_version=supervisor.snapshot.version,
                arguments=ControlArguments(autonomy="autonomous"),
            ),
        )
        assert supervisor.admit("reflection")
        assert not binding.admitted()

    run(scenario())


def test_role_cannot_reuse_a_different_kind_of_admission():
    async def scenario():
        fixture = await Fixture().setup()
        permit = fixture.supervisor.admit("observation")
        with pytest.raises(ValueError, match="M3_ROLE_ADMISSION_MISMATCH"):
            PinnedM3Control(fixture.supervisor, permit, "extractor")

    run(scenario())
