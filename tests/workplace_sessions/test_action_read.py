"""A successful action proof is exact, expiring, one-use and local to its task."""

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from ai_agent_application.jobs.ports import ExecutionPolicy
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Contract
from ai_agent_contracts.control import ExecutionCommand
from ai_agent_contracts.extensions import ExtensionRegistry

from ai_agent_core.m3_composition.action_read import ActionRead, CheckedAction
from ai_agent_core.m3_composition.policy import Policy
from ai_agent_core.m3_composition.schemas import PermitLimits


class Empty(Contract):
    pass


def command(identifier="command"):
    registry = ExtensionRegistry()
    registry.register("fixture.empty", Empty)
    now = datetime(2026, 10, 25, tzinfo=UTC)
    empty = dict(schema_id="fixture.empty", data={})
    return ExecutionCommand.model_validate(
        dict(
            id=identifier,
            created_at=now,
            type="execution.command",
            operation_id="operation",
            scope=dict(
                workspace_id="fixture",
                persona_id="person",
                memory_space_id="work",
                memory_mode="work",
                agent_id="agent",
                run_id="run",
            ),
            target="target",
            binding_id="binding",
            capability_id="m3.activity",
            capability_version=1,
            permission_id="permission",
            permission_version=1,
            arguments=empty,
            conditions=empty,
            deadline=now + timedelta(minutes=1),
            cancellation_id="cancel",
            freshness=dict(run_id="run", control_epoch=1, profile_version=1, policy_version=1),
        ),
        context={"extensions": registry},
    )


def approved(read, cmd, until=10):
    value = CheckedAction(MappingProxyType({}), PermitLimits())
    read.record(cmd, ExecutionPolicy(cmd.id, cmd.scope, True, until), value)
    return value


def test_action_is_consumed_once_and_a_new_action_needs_a_new_proof():
    async def scenario():
        cmd = command()
        read = ActionRead(cmd)
        value = approved(read, cmd)
        assert read.take(cmd, 1) is value
        with pytest.raises(ContractError):
            read.take(cmd, 1)
        with pytest.raises(ContractError):
            ActionRead(cmd).take(cmd, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["permission", "epoch", "scope", "expired", "nan"])
def test_changed_or_expired_command_cannot_use_the_read(change):
    async def scenario():
        cmd = command()
        read = ActionRead(cmd)
        approved(read, cmd)
        now = 1.0
        if change == "permission":
            cmd = cmd.model_copy(update={"permission_version": 2})
        elif change == "epoch":
            cmd = cmd.model_copy(
                update={"freshness": cmd.freshness.model_copy(update={"control_epoch": 2})}
            )
        elif change == "scope":
            cmd = cmd.model_copy(
                update={"scope": cmd.scope.model_copy(update={"persona_id": "other"})}
            )
        else:
            now = 10.0 if change == "expired" else float("nan")
        with pytest.raises(ContractError):
            read.take(cmd, now)

    asyncio.run(scenario())


def test_nested_or_child_task_cannot_supply_or_consume_another_actions_proof():
    async def scenario():
        # Only the capture mechanism is used here; no policy/permission is fabricated.
        policy = Policy.__new__(Policy)
        policy._action_read = ContextVar("fixture-action", default=None)
        cmd = command()
        with policy.action_read(cmd) as outer:

            async def child():
                assert policy._action_read.get() is outer
                approved(outer, cmd)
                with pytest.raises(ContractError):
                    outer.take(cmd, 1)

            await asyncio.create_task(child())
            with pytest.raises(ContractError):
                outer.take(cmd, 1)
            with policy.action_read(command("nested")) as inner:
                assert policy._action_read.get() is inner
            assert policy._action_read.get() is outer
            approved(outer, cmd)
            outer.take(cmd, 1)
        assert policy._action_read.get() is None

    asyncio.run(scenario())


def test_read_cannot_escape_its_action_context():
    async def scenario():
        policy = Policy.__new__(Policy)
        policy._action_read = ContextVar("fixture-closed", default=None)
        cmd = command()
        with policy.action_read(cmd) as read:
            approved(read, cmd)
        with pytest.raises(ContractError):
            read.take(cmd, 1)

    asyncio.run(scenario())
