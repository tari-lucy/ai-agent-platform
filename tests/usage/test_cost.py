"""Tariff versions, cost calculation and safe projections on real SQLite (M2-05)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from ai_agent_application.llm import ChatMessage, Failure, GenerationRequest, LLMGateway
from ai_agent_application.usage import (
    compute_cost,
    history,
    money,
    summarise,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.llm import CostCalculation, Tariff
from ai_agent_contracts.states import ErrorCode
from usage_support import (
    NOW,
    UNKNOWN,
    command,
    draft,
    make_scope,
    new_world,
    report,
    run,
    usage,
)

A = make_scope("persona-a")
A_TEST = make_scope("persona-a", "test")


async def priced(world, scope, item, **rates):
    await world.ledger.set_tariff(command(scope), draft(**rates))
    await world.record(scope, item)
    return await world.ledger.calculate(command(scope), item.attempt_id)


def test_cached_and_reasoning_tokens_are_priced_once_inside_their_parents(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        item = report(usage_=usage(1000, 500, cached=400, reasoning=100))
        return await priced(world, A, item, cached_input_rate="0.5", reasoning_output_rate="10")

    cost = run(scenario())
    # 600*2 + 400*0.5 + 400*6 + 100*10 = 1200+200+2400+1000 per 1000 tokens = 4.8
    assert cost.amount == "4.8" and cost.currency == "USD" and cost.completeness == "complete"
    assert cost.provenance == "provider_usage"
    naive = Decimal(1000 * 2 + 400 * 0.5 + 500 * 6 + 100 * 10) / 1000
    assert Decimal(cost.amount) != naive


def test_without_specific_rates_subsets_are_priced_at_the_parent_rate_never_twice(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        return await priced(world, A, report(usage_=usage(1000, 500, cached=400, reasoning=100)))

    # input 1000*2 + output 500*6 = 2000 + 3000 per 1000 = 5
    assert run(scenario()).amount == "5"


def test_a_missing_tariff_is_an_unknown_cost_not_zero(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report())
        cost = await world.ledger.calculate(command(A), "op-1.a1")
        return cost, await summarise(world.ledger, [A])

    cost, summary = run(scenario())
    assert cost.amount is None and cost.currency is None and cost.completeness == "unknown"
    assert cost.tariff_id is None
    assert summary.unknown_cost == 1 and summary.cost == {} and not summary.complete


def test_missing_usage_is_unknown_even_with_a_tariff(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        return await priced(world, A, report(status="cancelled", usage_=UNKNOWN))

    cost = run(scenario())
    assert cost.amount is None and cost.completeness == "unknown" and cost.tariff_id is not None


def test_partial_usage_gives_a_partial_lower_bound(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        return await priced(world, A, report(usage_=usage(1000, 0, partial=True)))

    cost = run(scenario())
    assert cost.completeness == "partial" and cost.amount == "2"  # only the known input part


def test_a_missing_rate_leaves_that_part_unpriced_and_the_result_partial(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        return await priced(world, A, report(), output_rate=None)

    cost = run(scenario())
    assert cost.completeness == "partial" and cost.amount == "2"


def test_no_rates_at_all_is_unknown(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        return await priced(world, A, report(), input_rate=None, output_rate=None)

    cost = run(scenario())
    assert cost.completeness == "unknown" and cost.amount is None


def test_zero_tokens_of_an_unpriced_category_are_a_real_zero():
    from ai_agent_contracts.llm import TokenUsage

    tariff = Tariff.model_validate(
        {
            "id": "t",
            "scope": A.model_dump(),
            "created_at": NOW,
            "version": 1,
            "updated_at": NOW,
            "connection_id": "c",
            "model_id": "m",
            "unit_tokens": 1000,
            "currency": "USD",
            "input_rate": "2",
            "output_rate": None,
            "effective_at": NOW,
        }
    )
    zero_output = TokenUsage(
        input_tokens=1000, output_tokens=0, source="provider", completeness="complete"
    )
    cost = compute_cost(zero_output, tariff)
    assert cost.completeness == "complete" and cost.amount == Decimal(2)


def test_local_estimate_is_marked_and_never_complete_provider_usage():
    from ai_agent_contracts.llm import TokenUsage

    estimate = TokenUsage(
        input_tokens=1000, output_tokens=500, source="local_estimate", completeness="complete"
    )
    tariff = Tariff.model_validate(
        {
            "id": "t",
            "scope": A.model_dump(),
            "created_at": NOW,
            "version": 1,
            "updated_at": NOW,
            "connection_id": "c",
            "model_id": "m",
            "unit_tokens": 1000,
            "currency": "USD",
            "input_rate": "2",
            "output_rate": "6",
            "effective_at": NOW,
        }
    )
    cost = compute_cost(estimate, tariff)
    assert cost.provenance == "local_estimate" and cost.amount == Decimal(5)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("0"), "0"),
        (Decimal("1E-7"), "0.0000001"),
        (Decimal("0.0000030000"), "0.000003"),
        (Decimal("12.50"), "12.5"),
        (Decimal("100"), "100"),
        (Decimal("0.123456789049"), "0.123456789"),
        (Decimal("1E+3"), "1000"),
    ],
)
def test_money_is_a_plain_decimal_string(value, text):
    assert money(value) == text


def test_a_changed_price_does_not_rewrite_past_costs(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        early = report("op-1.a1", operation="op-1", at=NOW)
        late = report("op-2.a1", operation="op-2", at=NOW + timedelta(days=2))
        await world.ledger.set_tariff(command(A), draft(effective=NOW - timedelta(days=1)))
        await world.record(A, early)
        first = await world.ledger.calculate(command(A), "op-1.a1")
        await world.ledger.set_tariff(
            command(A), draft(effective=NOW + timedelta(days=1), input_rate="4", output_rate="12")
        )
        await world.record(A, late)
        second = await world.ledger.calculate(command(A), "op-2.a1")
        again = await world.ledger.calculate(command(A), "op-1.a1")
        return first, second, again, await world.ledger.attempt(A, "op-1.a1")

    first, second, again, early = run(scenario())
    assert (first.amount, first.tariff_version) == ("5", 1)
    assert (second.amount, second.tariff_version) == ("10", 2)
    assert again == first and again.version == 1  # recalculating changes nothing silently
    assert early.tariff_version == 1  # the attempt remembers the terms that applied


def test_a_retroactive_price_correction_applies_only_with_an_explicit_repin(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.ledger.set_tariff(command(A), draft(effective=NOW - timedelta(days=2)))
        await world.record(A, report(at=NOW))  # runs under version 1
        first = await world.ledger.calculate(command(A), "op-1.a1")
        # a correction entered afterwards, valid from before the attempt started
        await world.ledger.set_tariff(
            command(A), draft(effective=NOW - timedelta(days=1), input_rate="4", output_rate="12")
        )
        kept = await world.ledger.calculate(command(A), "op-1.a1")
        fixed = await world.ledger.calculate(command(A), "op-1.a1", repin=True)
        return first, kept, fixed

    first, kept, fixed = run(scenario())
    assert kept == first and kept.tariff_version == 1 and kept.amount == "5"
    assert fixed.tariff_version == 2 and fixed.amount == "10" and fixed.version == 2


def test_a_tariff_added_later_applies_only_through_an_explicit_recalculation(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report(at=NOW))
        unknown = await world.ledger.calculate(command(A), "op-1.a1")
        await world.ledger.set_tariff(command(A), draft(effective=NOW - timedelta(days=30)))
        known = await world.ledger.calculate(command(A), "op-1.a1")
        history_ = await world.store.repository(CostCalculation, world.ledger.principal, A).get(
            unknown.id, version=1
        )
        return unknown, known, history_

    unknown, known, old = run(scenario())
    assert unknown.completeness == "unknown" and unknown.version == 1
    assert known.version == 2 and known.amount == "5" and known.completeness == "complete"
    assert old.completeness == "unknown"  # the earlier estimate stays inspectable


def test_a_tariff_that_starts_after_the_attempt_does_not_apply_back(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.ledger.set_tariff(command(A), draft(effective=NOW + timedelta(days=1)))
        await world.record(A, report(at=NOW))
        return await world.ledger.calculate(command(A), "op-1.a1")

    assert run(scenario()).amount is None


def test_tariff_history_is_append_only_and_idempotent(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        ledger = world.ledger
        one = await ledger.set_tariff(command(A), draft())
        same = await ledger.set_tariff(command(A), draft())
        two = await ledger.set_tariff(command(A), draft(input_rate="3"))
        with pytest.raises(ContractError) as info:
            await ledger.set_tariff(command(A), draft(effective=NOW - timedelta(days=9)))
        return one, same, two, info.value.error.code

    one, same, two, error = run(scenario())
    assert same.version == one.version == 1 and two.version == 2
    assert error is ErrorCode.INVALID_ARGUMENTS


def test_the_reported_model_is_priced_not_the_requested_one(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.ledger.set_tariff(command(A), draft(model="model-x"))
        await world.record(A, report(reported="routed-model"))  # no tariff for what actually ran
        unknown = await world.ledger.calculate(command(A), "op-1.a1")
        await world.ledger.set_tariff(
            command(A), draft(model="routed-model", input_rate="10", output_rate="10")
        )
        priced_ = await world.ledger.calculate(command(A), "op-1.a1", repin=True)
        return unknown, priced_

    unknown, priced_ = run(scenario())
    assert unknown.completeness == "unknown" and unknown.amount is None
    assert priced_.amount == "15" and priced_.version == 2


def test_currencies_are_never_added_together(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.ledger.set_tariff(command(A), draft(model="model-x", currency="USD"))
        await world.ledger.set_tariff(command(A), draft(model="model-y", currency="EUR"))
        await world.record(A, report("op-1.a1", operation="op-1"))
        await world.record(A, report("op-2.a1", operation="op-2", model="model-y"))
        for attempt in ("op-1.a1", "op-2.a1"):
            await world.ledger.calculate(command(A), attempt)
        return await summarise(world.ledger, [A])

    summary = run(scenario())
    assert set(summary.cost) == {"USD", "EUR"}
    assert summary.cost["USD"].amount_text == "5" and summary.cost["EUR"].amount_text == "5"
    assert summary.complete


def test_summary_separates_unknowns_and_never_double_counts_subsets(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, A_TEST])
        await world.ledger.set_tariff(command(A), draft())
        await world.ledger.set_tariff(command(A_TEST), draft())
        await world.record(
            A, report("op-1.a1", operation="op-1", usage_=usage(1000, 500, 400, 100))
        )
        await world.record(A, report("op-1.a2", operation="op-1", status="unknown", usage_=UNKNOWN))
        await world.record(A_TEST, report("op-9.a1", operation="op-9", usage_=usage(10, 5)))
        for scope, attempt in ((A, "op-1.a1"), (A, "op-1.a2"), (A_TEST, "op-9.a1")):
            await world.ledger.calculate(command(scope), attempt)
        return (
            await summarise(world.ledger, [A, A_TEST]),
            await summarise(world.ledger, [A]),
            await summarise(world.ledger, [A, A_TEST], role_id="nobody"),
            await summarise(world.ledger, [A, A_TEST], until=NOW),
        )

    both, work, none, early = run(scenario())
    assert both.totals.attempts == 3 and work.totals.attempts == 2
    assert set(both.by_mode) == {"work", "test"}
    assert both.by_mode["test"].attempts == 1 and both.by_mode["work"].attempts == 2
    assert work.totals.input_tokens == 1000 and work.totals.cached_input_tokens == 400
    assert work.totals.output_tokens == 500 and work.totals.reasoning_output_tokens == 100
    assert work.totals.retries == 1 and work.totals.operations == 1
    assert work.totals.unknown_usage == 1 and not work.totals.complete and not work.complete
    assert work.unknown_cost == 1 and work.cost["USD"].amount_text == "5"
    assert none.totals.attempts == 0 and early.totals.attempts == 0
    assert both.by_role["character"].attempts == 3 and both.by_connection["conn-a"].attempts == 3


def test_history_lists_every_attempt_newest_first_with_its_cost(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.record(A, report("op-1.a1", operation="op-1", at=NOW))
        await world.record(
            A,
            report(
                "op-2.a1",
                operation="op-2",
                at=NOW + timedelta(minutes=1),
                status="cancelled",
                usage_=UNKNOWN,
            ),
        )
        await world.ledger.calculate(command(A), "op-2.a1")
        return await history(world.ledger, A, limit=10)

    views = run(scenario())
    assert [v.attempt.attempt_id for v in views] == ["op-2.a1", "op-1.a1"]
    assert views[0].attempt.status == "cancelled" and views[0].cost.completeness == "unknown"
    assert views[1].cost is None


def test_gateway_reports_flow_into_the_ledger_including_cancellation_and_late_usage(tmp_path):
    import asyncio

    from ai_agent_adapters.llm import ScriptedProvider
    from ai_agent_adapters.llm.mock import Pause
    from ai_agent_application.llm import (
        Delta,
        ProviderError,
        Started,
        register_llm_extensions,
        resolve_assignment,
    )
    from ai_agent_contracts.extensions import ExtensionRegistry
    from ai_agent_contracts.llm import Connection, LLMAssignment

    registry = ExtensionRegistry()
    register_llm_extensions(registry)
    connection = Connection.model_validate(
        {
            "id": "conn-a",
            "scope": A.model_dump(),
            "created_at": NOW,
            "version": 1,
            "updated_at": NOW,
            "name": "conn-a",
            "adapter_id": "mock",
            "adapter_version": "1",
            "endpoint_setting": "AI_PLATFORM_E",
            "credential_setting": "AI_PLATFORM_K",
            "configured": True,
            "verification": "unverified",
            "capabilities": [],
        },
        context={"extensions": registry},
    )
    assignment = LLMAssignment.model_validate(
        {
            "id": "asg-a",
            "scope": A.model_dump(),
            "created_at": NOW,
            "version": 1,
            "updated_at": NOW,
            "role_id": "character",
            "connection_id": "conn-a",
            "model_id": "model-x",
            "parameters": {
                "schema_id": "llm.generation-parameters",
                "data": {"max_output_tokens": 64, "timeout_seconds": 5},
            },
            "required_capabilities": [],
            "capability_verification": "unverified",
        },
        context={"extensions": registry},
    )
    resolved = resolve_assignment(assignment, connection, registry)

    async def scenario():
        world = await new_world(tmp_path, [A])
        collected = []
        provider = ScriptedProvider(
            [
                [ProviderError(Failure.RATE_LIMITED)],  # attempt 1: a real failed call
                [Started("model-x"), Delta("ок"), Pause(30)],  # attempt 2: cancelled by STOP
            ]
        )
        cancel = asyncio.Event()

        async def sleep(_):
            return None

        async def on_delta(_text):
            cancel.set()

        gateway = LLMGateway(
            lambda _a: provider,
            lambda _r: resolved,
            sink=collected.append,
            sleep=sleep,
            jitter=lambda: 1.0,
        )
        request = GenerationRequest("op-7", "character", (ChatMessage("user", "Привет"),))
        result = await gateway.generate(request, cancel=cancel, on_delta=on_delta)
        for item in collected:
            await world.record(A, item)
            await world.record(A, item)  # a delivered-twice receipt
        stored = await world.ledger.attempts(A)
        # the provider's final usage arrives after STOP and is still accounted
        late = report(
            "op-7.a2",
            operation="op-7",
            status="cancelled",
            usage_=usage(30, 2),
            at=collected[1].started_at,
        )
        late = type(late)(**{**late.__dict__, "completed_at": collected[1].completed_at})
        await world.record(A, late)
        return result, stored, await world.ledger.attempt(A, "op-7.a2")

    result, stored, late = run(scenario())
    assert result.status == "cancelled" and len(stored) == 2
    assert {a.status for a in stored} == {"failed", "cancelled"}
    assert all(a.usage.source == "unknown" for a in stored)
    assert late.usage.input_tokens == 30 and late.version == 2
