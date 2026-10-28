"""Resource limits and original control identity; no live API/executor permissions."""

import asyncio
import time
from dataclasses import replace
from threading import Thread

import pytest
from ai_agent_application.scheduling import (
    BudgetState,
    Demand,
    Lane,
    Pool,
    ResourceScheduler,
    SchedulerLimits,
)
from ai_agent_application.telemetry import HealthState, Outcome
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState
from operations_support import fixture


def used(f, identifier):
    return next(item.used for item in f.scheduler.usage() if item.pool.id == identifier)


def test_three_normal_calls_leave_actual_fourth_slot_for_moderation():
    f = fixture()
    normal = f.capability("normal", {"llm": 1, "cpu": 100, "ram": 64})
    moderator = f.capability("moderator", {"llm": 1, "cpu": 100, "ram": 64}, Lane.MODERATION)
    leases = [f.acquire(normal) for _ in range(3)]
    waiting = f.scheduler.request(f.command(normal))
    assert f.scheduler.acquire(waiting) is None
    moderation = f.acquire(moderator)
    assert used(f, "llm") == 4
    assert f.scheduler.decision(waiting).error == ErrorCode.RESOURCE_BUSY
    for lease in [*leases, moderation]:
        f.scheduler.complete(lease, Outcome.SUCCEEDED)
    allocation = f.scheduler.acquire(waiting)
    assert allocation and allocation.acquired


def test_cpu_ram_reservations_preserve_moderation_when_normal_budget_full():
    f = fixture()
    heavy = f.capability("heavy", {"cpu": 1800, "ram": 1408})
    mod = f.capability("moderator", {"cpu": 200, "ram": 128, "llm": 1}, Lane.MODERATION)
    f.acquire(heavy)
    f.acquire(mod)
    assert used(f, "cpu") == 2000 and used(f, "ram") == 1536
    with pytest.raises(ContractError):
        f.capability("too-heavy", {"cpu": 2000})
    with pytest.raises(ContractError):
        f.capability("too-large-moderator", {"ram": 129}, Lane.MODERATION)


def test_foreground_embedding_gets_next_batch_before_background():
    f = fixture()
    background = f.capability("index", {"embedding": 1, "background": 1}, Lane.BACKGROUND)
    foreground = f.capability("recall", {"embedding": 1})
    first = f.acquire(background)
    queued_background = f.scheduler.request(f.command(background))
    queued_foreground = f.scheduler.request(f.command(foreground))
    f.scheduler.complete(first, Outcome.SUCCEEDED)
    assert f.scheduler.acquire(queued_background) is None
    next_batch = f.scheduler.acquire(queued_foreground)
    assert next_batch and next_batch.acquired
    f.scheduler.complete(next_batch.lease, Outcome.SUCCEEDED)
    assert f.scheduler.acquire(queued_background)


def test_background_and_foreground_turn_have_independent_finite_slots():
    f = fixture()
    bg = f.capability("reflection", {"background": 1}, Lane.BACKGROUND)
    fg = f.capability("dialogue", {"foreground": 1})
    f.acquire(bg)
    assert f.scheduler.acquire(f.scheduler.request(f.command(bg))) is None
    f.acquire(fg)
    assert f.scheduler.acquire(f.scheduler.request(f.command(fg))) is None


def test_duplicate_request_allocation_and_receipt_do_not_restart_or_double_free():
    f = fixture()
    cap = f.capability("normal", {"llm": 1})
    command = f.command(cap)
    request = f.scheduler.request(command)
    assert f.scheduler.request(command) is request
    allocation = f.scheduler.acquire(request)
    assert allocation and allocation.acquired
    repeated = f.scheduler.acquire(request)
    assert repeated and not repeated.acquired and repeated.lease is allocation.lease
    assert used(f, "llm") == 1
    result = f.scheduler.complete(allocation.lease, Outcome.SUCCEEDED)
    assert f.scheduler.complete(allocation.lease, Outcome.SUCCEEDED) is result
    assert used(f, "llm") == 0
    assert f.scheduler.request(command) is request
    with pytest.raises(ContractError):
        f.scheduler.acquire(request)
    with pytest.raises(ContractError):
        f.scheduler.request(command.model_copy(update={"binding_id": "altered-binding"}))
    with pytest.raises(ContractError):
        f.scheduler.complete(allocation.lease, Outcome.FAILED)


def test_queue_backpressure_and_stop_do_not_wait_for_capacity_or_receipts():
    f = fixture(limits=SchedulerLimits(pending=2))
    cap = f.capability("normal", {"foreground": 1})
    live = f.acquire(cap)
    pending = [f.scheduler.request(f.command(cap)) for _ in range(2)]
    with pytest.raises(ContractError):
        f.scheduler.request(f.command(cap))
    start = time.monotonic()
    f.stop()
    f.scheduler.poll()
    assert time.monotonic() - start < 0.25
    assert f.supervisor.snapshot.state == RunState.STOPPING
    assert all(f.scheduler.decision(req).outcome == Outcome.CANCELLED for req in pending)
    assert used(f, "foreground") == 1
    with pytest.raises(ContractError):
        f.scheduler.verify(live)
    f.scheduler.complete(live, Outcome.CANCELLED)
    assert used(f, "foreground") == 0 and f.supervisor.snapshot.state == RunState.STOPPED


def test_unknown_outcome_does_not_free_or_claim_resource_terminated():
    f = fixture()
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    unknown = f.scheduler.complete(lease, Outcome.UNKNOWN)
    assert unknown.state == BudgetState.UNKNOWN and used(f, "foreground") == 1
    assert f.supervisor.snapshot.unconfirmed_resource_ids
    waiting = f.scheduler.request(f.command(cap))
    assert f.scheduler.acquire(waiting) is None
    with pytest.raises(ContractError):
        f.scheduler.verify(lease)
    f.scheduler.complete(lease, Outcome.SUCCEEDED)  # authenticated factual reconciliation
    assert used(f, "foreground") == 0 and f.scheduler.acquire(waiting)
    assert any(item.outcome == Outcome.UNKNOWN for item in f.telemetry.traces(lease.request.scope))


def test_missing_cancel_receipt_becomes_unknown_then_late_receipt_settles_only():
    f = fixture()
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    f.stop()
    assert f.scheduler.decision(lease.request).state == BudgetState.CANCELLING
    f.time.advance(30.1)  # past the 30 s cancellation grace
    f.scheduler.poll()
    assert f.scheduler.decision(lease.request).state == BudgetState.UNKNOWN
    assert f.supervisor.snapshot.state == RunState.FAULTED
    assert used(f, "foreground") == 1
    f.scheduler.complete(lease, Outcome.CANCELLED)
    assert used(f, "foreground") == 0
    assert f.supervisor.snapshot.state != RunState.ACTIVE


def test_queue_wait_deadline_is_explicit_and_not_extended_by_duplicate():
    f = fixture(limits=SchedulerLimits(wait_seconds=2))
    cap = f.capability("normal", {"foreground": 1})
    f.acquire(cap)
    command = f.command(cap)
    request = f.scheduler.request(command)
    f.time.advance(30.1)  # past the 30 s cancellation grace
    decision = f.scheduler.decision(request)
    assert decision.outcome == Outcome.EXPIRED and decision.error == ErrorCode.EXPIRED
    assert f.scheduler.request(command) is request
    with pytest.raises(ContractError):
        f.scheduler.acquire(request)


def test_actual_execution_deadline_is_separate_from_queue_wait_timeout():
    f = fixture(limits=SchedulerLimits(wait_seconds=1))
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    f.time.advance(2)
    f.scheduler.verify(lease)
    assert f.scheduler.decision(lease.request).state == BudgetState.RUNNING


def test_expired_admission_closes_lease_but_does_not_free_resource():
    f = fixture()
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    f.time.advance(61)  # past the 60 s admission
    with pytest.raises(ContractError):
        f.scheduler.verify(lease)
    assert used(f, "foreground") == 1
    f.scheduler.complete(lease, Outcome.CANCELLED)


def test_refresh_original_cancellation_id_keeps_budget_usable():
    f = fixture()
    cap = f.capability("normal", {"llm": 1})
    lease = f.acquire(cap)
    f.time.advance(5)
    admission = f.supervisor.resolve(
        lease.request.cancellation_id,
        scope=lease.request.scope,
        freshness=lease.request.freshness,
    )
    renewed = asyncio.run(f.supervisor.refresh(admission))
    assert renewed.id == lease.request.cancellation_id
    f.time.advance(16)
    f.scheduler.verify(lease)
    f.scheduler.complete(lease, Outcome.SUCCEEDED)


def test_health_unavailable_waits_until_fresh_ready_and_stale_health_closes_execution():
    f = fixture()
    f.telemetry.register("embedder")
    cap = f.capability("recall", {"embedding": 1}, components=("embedder",))
    request = f.scheduler.request(f.command(cap))
    assert f.scheduler.acquire(request) is None
    assert f.scheduler.decision(request).error == ErrorCode.CAPABILITY_UNAVAILABLE
    f.telemetry.update_health("embedder", HealthState.READY)
    allocation = f.scheduler.acquire(request)
    assert allocation
    f.telemetry.update_health("embedder", HealthState.UNAVAILABLE)
    with pytest.raises(ContractError):
        f.scheduler.verify(allocation.lease)
    assert used(f, "embedding") == 1


def test_forged_scope_epoch_and_capability_version_are_not_scheduled():
    f = fixture()
    cap = f.capability("normal", {"llm": 1})
    command = f.command(cap)
    changed_scope = command.scope.model_copy(update={"memory_mode": "work"})
    for altered in [
        command.model_copy(update={"scope": changed_scope}),
        command.model_copy(update={"capability_version": 2}),
        command.model_copy(update={"cancellation_id": "unknown"}),
        command.model_copy(
            update={
                "freshness": command.freshness.model_copy(
                    update={"control_epoch": command.freshness.control_epoch + 1}
                )
            }
        ),
    ]:
        with pytest.raises(ContractError):
            f.scheduler.request(altered)
    assert used(f, "llm") == 0


def test_registry_version_change_invalidates_old_queue_and_active():
    f = fixture()
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    request = f.scheduler.request(f.command(cap))
    f.capability("normal", {"foreground": 1}, version=2)
    assert f.scheduler.decision(request).outcome == Outcome.CANCELLED
    with pytest.raises(ContractError):
        f.scheduler.verify(lease)
    assert used(f, "foreground") == 1


def test_forged_budget_token_and_other_thread_are_refused():
    f = fixture()
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    with pytest.raises(ContractError):
        f.scheduler.complete(replace(lease), Outcome.CANCELLED)
    with pytest.raises(ContractError):
        f.scheduler.acquire(replace(lease.request))
    errors = []

    def mutate():
        try:
            f.scheduler.usage()
        except RuntimeError as error:
            errors.append(str(error))

    thread = Thread(target=mutate)
    thread.start()
    thread.join()
    assert errors == ["SCHEDULER_OWNER_THREAD_REQUIRED"]


def test_restart_new_scheduler_never_grants_old_request_or_admission():
    f = fixture()
    cap = f.capability("normal", {"foreground": 1})
    lease = f.acquire(cap)
    with pytest.raises(ContractError):
        ResourceScheduler(f.supervisor, f.telemetry)
    with pytest.raises(ContractError):
        f.scheduler.acquire(replace(lease.request))
    # Process restart requires a NEW stopped supervisor + recover/reconcile.
    assert f.supervisor.snapshot.unconfirmed_resource_ids


@pytest.mark.parametrize(
    "build",
    [
        lambda: Pool("cpu", True),
        lambda: Pool("cpu", 0),
        lambda: Demand("cpu", 0),
        lambda: Pool("cpu", 2, Lane.MODERATION, 3),
        lambda: Pool("cpu", 2, None, 1),
        lambda: SchedulerLimits(pending=257),
        lambda: SchedulerLimits(wait_seconds=float("inf")),
    ],
)
def test_invalid_limits_or_demands_fail_closed(build):
    with pytest.raises(ValueError):
        build()


def test_bounded_history_evicts_queries_without_authorizing_old_token():
    f = fixture(limits=SchedulerLimits(history=1))
    cap = f.capability("normal", {"foreground": 1})
    first, second = f.acquire(cap), None
    f.scheduler.complete(first, Outcome.SUCCEEDED)
    second = f.acquire(cap)
    f.scheduler.complete(second, Outcome.FAILED)
    with pytest.raises(ContractError):
        f.scheduler.decision(first.request)
    with pytest.raises(ContractError):
        f.scheduler.verify(first)


def test_health_recovery_does_not_revive_cancelled_budget_or_hide_unknown():
    f = fixture()
    f.telemetry.register("provider")
    f.telemetry.update_health("provider", HealthState.READY)
    cap = f.capability("normal", {"llm": 1}, components=("provider",))
    lease = f.acquire(cap)
    f.telemetry.update_health("provider", HealthState.UNAVAILABLE)
    assert f.scheduler.decision(lease.request).state == BudgetState.CANCELLING
    f.telemetry.update_health("provider", HealthState.READY)
    f.time.advance(30.1)  # past the 30 s cancellation grace
    assert f.scheduler.decision(lease.request).state == BudgetState.UNKNOWN
    assert used(f, "llm") == 1


def test_confirmed_new_attempt_gets_new_budget_but_unknown_blocks_another_attempt():
    f = fixture()
    cap = f.capability("normal", {"llm": 1})
    command = f.command(cap)
    first = f.scheduler.request(command, attempt_id="first-attempt")
    allocation = f.scheduler.acquire(first)
    assert allocation
    f.scheduler.complete(allocation.lease, Outcome.FAILED)
    second = f.scheduler.request(command, attempt_id="second-attempt")
    assert second is not first and second.attempt_id == "second-attempt"
    second_allocation = f.scheduler.acquire(second)
    assert second_allocation and second_allocation.acquired
    f.scheduler.complete(second_allocation.lease, Outcome.UNKNOWN)
    with pytest.raises(ContractError) as error:
        f.scheduler.request(command, attempt_id="third-attempt")
    assert error.value.error.code == ErrorCode.OUTCOME_UNKNOWN
