"""The coordinator ports preserve ordinary TTL/ACL and roll back revoked writes."""

import pytest
from ai_agent_application.storage import Principal
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Source
from ai_agent_contracts.persona import StoryArc
from ai_agent_contracts.states import ErrorCode
from gm_support import GM, new_world, run


def test_fence_revoked_after_staged_put_rolls_back_entire_transaction(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        result, _ = await world.arc()
        arc = result.arc
        open_ = True

        def guard():
            if not open_:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        with pytest.raises(ContractError):
            with world.store.fence(guard):
                async with world.store.transaction(GM, world.scope, world.command()) as unit:
                    revised = world.gm._revised(arc, version=2, status="paused")
                    await unit.put(revised, expected_version=1)
                    open_ = False
                    # complete/finish must recheck, not merely transaction entry.
                    from ai_agent_contracts.api import Receipt

                    await unit.complete(
                        Receipt(
                            scope=world.scope,
                            request_id=unit.command.request_id,
                            status="completed",
                            resource_id=arc.id,
                            version=2,
                        )
                    )
        stored = await world.store.repository(StoryArc, GM, world.scope).get(arc.id)
        await world.store.close()
        return stored

    stored = run(scenario())
    assert stored.version == 1 and stored.status == "open"


def test_lifecycle_port_is_not_an_expired_source_or_operator_read_bypass(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        source = await world.source()
        for model, principal, identifier in [
            (Source, GM, source.source_id),
            (StoryArc, GM, "unknown"),
            (Source, Principal("operator", "operator"), source.source_id),
        ]:
            with pytest.raises(ContractError) as denied:
                await world.store.repository(model, principal, world.scope).get_for_lifecycle(
                    identifier
                )
            assert denied.value.error.code is ErrorCode.ACCESS_DENIED
        await world.store.close()

    run(scenario())


def test_nested_fences_cannot_restore_revoked_admission(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)

        def deny():
            raise ContractError(ErrorCode.STALE_CONTEXT)

        with world.store.fence(deny):
            with world.store.fence(lambda: None):
                with pytest.raises(ContractError):
                    world.store.check_fence()
        world.store.check_fence()
        await world.store.close()

    run(scenario())

def test_owner_fence_keeps_late_llm_usage_but_blocks_character_mutation(tmp_path):
    from ai_agent_application.storage import Owner
    from usage_support import command, make_scope, new_world as usage_world, report

    async def scenario():
        scope = make_scope("persona-a")
        world = await usage_world(tmp_path, [scope])

        def stopped():
            raise ContractError(ErrorCode.STALE_CONTEXT)

        with world.store.fence(stopped, owner=Owner.GM):
            written = await world.record(scope, report())
            assert written.created
            with pytest.raises(ContractError) as denied:
                async with world.store.transaction(GM, scope, command(scope)):
                    raise AssertionError("revoked owner entered a transaction")
            assert denied.value.error.code is ErrorCode.STALE_CONTEXT
        return await world.ledger.attempts(scope)

    assert len(run(scenario())) == 1
