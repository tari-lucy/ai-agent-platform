"""Trusted optional session validity is checked after awaited readiness."""

import asyncio
from dataclasses import replace

import pytest
from ai_agent_contracts.api import ControlArguments, ControlCommand, StopCommand
from ai_agent_contracts.codec import ContractError
from operations_support import Fixture


def test_optional_authority_revocation_before_stop():
    async def scenario():
        f = await Fixture().setup()
        authority = replace(f.operator, is_current=lambda: False)
        epoch = f.supervisor.snapshot.control_epoch
        with pytest.raises(ContractError):
            f.supervisor.stop(
                authority,
                StopCommand(
                    type="agent.stop",
                    request_id="stop",
                    target="synthetic-agent",
                    scope=f.supervisor.snapshot.scope,
                ),
            )
        assert f.supervisor.snapshot.control_epoch == epoch

    asyncio.run(scenario())


def test_authority_revocation_after_await_cannot_open_admission():
    async def scenario():
        f = await Fixture().setup()
        f.supervisor.stop(
            f.operator,
            StopCommand(
                type="agent.stop",
                request_id="stop",
                target="synthetic-agent",
                scope=f.supervisor.snapshot.scope,
            ),
        )
        await f.supervisor.flush()
        entered, release = asyncio.Event(), asyncio.Event()
        original = f.supervisor.readiness

        class Delayed:
            async def check(self, run):
                entered.set()
                await release.wait()
                return await original.check(run)

            async def reconcile(self, previous):
                return ()

        f.supervisor.readiness = Delayed()
        valid = True
        authority = replace(f.operator, is_current=lambda: valid)
        command = ControlCommand(
            type="agent.start",
            request_id="restart",
            target="synthetic-agent",
            scope=f.supervisor.snapshot.scope,
            expected_version=f.supervisor.snapshot.version,
            arguments=ControlArguments(autonomy="autonomous"),
        )
        task = asyncio.create_task(f.supervisor.control(authority, command))
        await entered.wait()
        valid = False
        release.set()
        with pytest.raises(ContractError):
            await task
        assert f.supervisor.snapshot.state == "FAULTED"
        with pytest.raises(ContractError):
            f.supervisor.admit("activity")

    asyncio.run(scenario())
