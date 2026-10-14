"""Attempt ledger on real SQLite (M2-05): one paid attempt, honest usage, late observations."""

import asyncio
from datetime import timedelta

import pytest
from ai_agent_application.storage import Owner, Principal
from ai_agent_application.usage import UsageLedger, attempt_record_id, merge_usage
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.llm import LLMAttempt
from ai_agent_contracts.states import ErrorCode
from usage_support import (
    NOW,
    UNKNOWN,
    WORKER,
    command,
    make_scope,
    new_world,
    report,
    run,
    usage,
)

A = make_scope("persona-a")
A_TEST = make_scope("persona-a", "test")
B = make_scope("persona-b")


def code(info):
    return info.value.error.code


def test_a_repeated_receipt_is_not_a_new_attempt(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        first = await world.record(A, report())
        again = await world.record(A, report())  # another command, same attempt
        replay_cmd = command(A, "req-fixed")
        one = await world.ledger.record_attempt(replay_cmd, report("op-1.a2", operation="op-1"))
        two = await world.ledger.record_attempt(replay_cmd, report("op-1.a2", operation="op-1"))
        return first, again, one, two, await world.ledger.attempts(A)

    first, again, one, two, stored = run(scenario())
    assert first.created and first.attempt.version == 1
    assert not again.created and not again.changed and again.attempt.version == 1
    assert one.created and not two.created and not two.changed
    assert sorted(a.attempt_id for a in stored) == ["op-1.a1", "op-1.a2"]


def test_concurrent_reports_of_one_attempt_make_one_record(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        results = await asyncio.gather(*(world.record(A, report()) for _ in range(6)))
        return results, await world.ledger.attempts(A)

    results, stored = run(scenario())
    assert len(stored) == 1 and sum(r.created for r in results) == 1


def test_a_real_retry_is_a_second_attempt_with_its_own_record(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report("op-1.a1", status="failed", usage_=UNKNOWN))
        await world.record(A, report("op-1.a2"))
        return await world.ledger.attempts(A)

    stored = run(scenario())
    assert {(a.attempt_id, a.status) for a in stored} == {
        ("op-1.a1", "failed"),
        ("op-1.a2", "completed"),
    }


def test_late_final_usage_replaces_the_unknown_one_and_is_never_added(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report(status="cancelled", usage_=UNKNOWN))
        late = await world.record(A, report(status="cancelled", usage_=usage(800, 40)))
        weaker = await world.record(A, report(status="cancelled", usage_=UNKNOWN))
        return late, weaker, await world.ledger.attempt(A, "op-1.a1")

    late, weaker, stored = run(scenario())
    assert late.changed and late.attempt.version == 2
    assert (stored.usage.input_tokens, stored.usage.output_tokens) == (800, 40)
    assert stored.status == "cancelled"  # the late usage does not rewrite how the attempt ended
    assert not weaker.changed and stored.version == 2  # nothing downgrades a known usage


def test_a_partial_usage_is_upgraded_by_the_final_one_without_summing():
    partial = usage(300, 0, partial=True)
    final = usage(1000, 200)
    assert merge_usage(partial, final) == final
    assert merge_usage(final, partial) == final
    assert merge_usage(UNKNOWN, partial) == partial


def test_two_different_complete_provider_usages_are_a_conflict():
    with pytest.raises(ContractError) as info:
        merge_usage(usage(100, 10), usage(100, 11))
    assert info.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert merge_usage(usage(100, 10), usage(100, 10)) == usage(100, 10)


def test_unknown_attempt_becomes_terminal_but_a_terminal_one_is_not_rewritten(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report(status="unknown", usage_=UNKNOWN))
        done = await world.record(A, report(status="completed", usage_=usage()))
        later = await world.record(A, report(status="cancelled", usage_=usage()))
        return done, later, await world.ledger.attempt(A, "op-1.a1")

    done, later, stored = run(scenario())
    assert done.changed and stored.status == "completed"
    assert not later.changed


def test_the_same_attempt_id_with_other_identity_is_rejected(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report())
        for other in (
            report(model="model-other"),
            report(role="game-master"),
            report(operation="op-9"),
            report(assignment="asg-z"),
            report(at=NOW + timedelta(seconds=5)),
        ):
            with pytest.raises(ContractError) as info:
                await world.record(A, other)
            assert code(info) is ErrorCode.IDEMPOTENCY_CONFLICT

    run(scenario())


def test_the_reported_model_is_learned_once_and_not_overwritten(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report(reported=None))
        await world.record(A, report(reported="routed-1"))
        await world.record(A, report(reported="routed-2"))
        return await world.ledger.attempt(A, "op-1.a1")

    stored = run(scenario())
    assert stored.reported_model_id == "routed-1" and stored.requested_model_id == "model-x"


def test_work_test_and_persona_scopes_are_isolated(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, A_TEST, B])
        await world.record(A, report())
        await world.record(
            A_TEST, report()
        )  # the same attempt id in the test scope: its own record
        return (
            [a.attempt_id for a in await world.ledger.attempts(A)],
            [a.attempt_id for a in await world.ledger.attempts(A_TEST)],
            await world.ledger.attempts(B),
            await world.ledger.attempt(B, "op-1.a1"),
        )

    work, test, other, none = run(scenario())
    assert work == test == ["op-1.a1"] and other == [] and none is None


def test_only_the_llm_owner_may_write_or_read_the_ledger(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report())
        record_id = attempt_record_id(A, "op-1.a1")
        with pytest.raises(ContractError) as info:
            await world.store.repository(LLMAttempt, WORKER, A).get(record_id)
        return code(info)

    assert run(scenario()) is ErrorCode.ACCESS_DENIED
    with pytest.raises(ContractError):
        UsageLedger(None, Principal("p", "core_owner", Owner.MEMORY), None)  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        UsageLedger(None, WORKER, None)  # type: ignore[arg-type]


def test_the_ledger_survives_a_restart(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report(usage_=usage(10, 5)))
        fresh = world.restarted()
        again = await fresh.record_attempt(command(A), report(usage_=usage(10, 5)))
        return again, await fresh.attempt(A, "op-1.a1")

    again, stored = run(scenario())
    assert not again.created and stored.usage.input_tokens == 10


def test_no_prompt_text_endpoint_or_secret_field_exists_in_the_record():
    names = set(LLMAttempt.model_fields)
    assert not names & {"prompt", "messages", "text", "reply", "endpoint", "credential", "headers"}
