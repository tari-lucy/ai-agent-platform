"""Bounded projections, safe correlation and slow-client failure, not financial records."""

import asyncio
from dataclasses import asdict, replace
from threading import Thread

import pytest
from ai_agent_application.telemetry import HealthState, Metric, Outcome, Telemetry, TelemetryLimits
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState
from operations_support import BASE, Time, fixture


def test_latest_coalesces_and_bounded_eviction_is_visible():
    telemetry = Telemetry(limits=TelemetryLimits(latest=2))
    telemetry.register("fixture")
    for n in range(10000):
        telemetry.emit(BASE, "fixture", Metric.QUEUED, n)
    assert len(telemetry.snapshot(BASE)) == 1
    assert telemetry.snapshot(BASE)[0].value == 9999
    telemetry.emit(BASE, "fixture", Metric.RUNNING, 1)
    telemetry.emit(BASE, "fixture", Metric.WAITING, 1)
    assert len(telemetry.snapshot(BASE)) == 2 and telemetry.dropped_latest == 1


def test_slow_client_disconnects_without_blocking_other_client():
    telemetry = Telemetry(limits=TelemetryLimits(client_events=2))
    telemetry.register("fixture")
    slow, fast = telemetry.subscribe(BASE), telemetry.subscribe(BASE)
    for _ in range(3):
        telemetry.emit(BASE, "fixture", Metric.QUEUED, 1)
        assert len(fast.drain()) == 1
    assert slow.closed and slow.error == ErrorCode.RESOURCE_BUSY
    assert not fast.closed and telemetry.disconnected_clients == 1
    with pytest.raises(ContractError):
        slow.drain()
    telemetry.unsubscribe(fast)
    assert fast.closed


def test_scope_run_and_work_test_isolation():
    telemetry = Telemetry()
    telemetry.register("fixture")
    other = BASE.model_copy(update={"memory_mode": "work"})
    client = telemetry.subscribe(other)
    telemetry.emit(BASE, "fixture", Metric.QUEUED, 1)
    assert client.drain() == () and telemetry.snapshot(other) == ()
    trace = telemetry.begin(BASE, "fixture")
    telemetry.end(trace, Outcome.SUCCEEDED)
    assert telemetry.traces(other) == ()
    run = BASE.model_copy(update={"run_id": "different-run"})
    assert telemetry.snapshot(run) == ()


def test_trace_correlates_opaque_ids_without_raw_prompt_key_or_endpoint():
    time = Time()
    telemetry = Telemetry(clock=time.clock())
    telemetry.register("fixture")
    marker = "synthetic-canary-do-not-export"
    trace = telemetry.begin(
        BASE, "fixture", request_id=marker, operation_id=marker, attempt_id=marker
    )
    time.advance(3)
    result = telemetry.end(trace, Outcome.UNKNOWN)
    assert result.elapsed_seconds == 3
    assert telemetry.end(trace, Outcome.UNKNOWN) is result
    rendered = repr(asdict(result)) + repr(telemetry.snapshot(BASE))
    assert marker not in rendered and BASE.persona_id not in rendered
    assert result.outcome == Outcome.UNKNOWN
    with pytest.raises(ContractError):
        telemetry.end(trace, Outcome.SUCCEEDED)
    with pytest.raises(TypeError):
        telemetry.emit(BASE, "fixture", Metric.QUEUED, 1, prompt=marker)
    with pytest.raises(ContractError):
        telemetry.emit(BASE, "fixture", marker, 1)


def test_active_traces_and_history_have_separate_bounded_backpressure():
    telemetry = Telemetry(limits=TelemetryLimits(traces=2))
    telemetry.register("fixture")
    first, second = telemetry.begin(BASE, "fixture"), telemetry.begin(BASE, "fixture")
    with pytest.raises(ContractError):
        telemetry.begin(BASE, "fixture")
    telemetry.end(first, Outcome.FAILED)
    telemetry.end(second, Outcome.CANCELLED)
    third = telemetry.begin(BASE, "fixture")
    telemetry.end(third, Outcome.SUCCEEDED)
    assert len(telemetry.traces(BASE)) == 2
    with pytest.raises(ContractError):
        telemetry.end(first, Outcome.FAILED)


def test_stale_optional_health_is_visible_without_global_fault():
    f = fixture()
    f.telemetry.register("optional")
    f.telemetry.update_health("optional", HealthState.READY)
    f.time.advance(21)
    f.telemetry.poll()
    assert f.telemetry.health("optional").state == HealthState.UNAVAILABLE
    assert f.supervisor.snapshot.state == RunState.ACTIVE


def test_required_health_failure_revokes_and_recovery_does_not_resume():
    f = fixture()
    f.telemetry.register("required", critical=True)
    f.telemetry.update_health("required", HealthState.READY)
    admission = f.supervisor.admit("activity")
    f.time.advance(21)
    f.telemetry.poll()
    assert admission.cancelled.is_set()
    assert f.supervisor.snapshot.state == RunState.FAULTED
    f.telemetry.update_health("required", HealthState.READY)
    assert f.supervisor.snapshot.state == RunState.FAULTED


@pytest.mark.parametrize(
    "limits",
    [
        {"latest": 0},
        {"clients": 33},
        {"traces": True},
        {"client_events": 1.5},
        {"health_seconds": float("nan")},
        {"components": 65},
    ],
)
def test_invalid_telemetry_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        TelemetryLimits(**limits)


def test_component_and_subscription_limits_and_closed_enum_fields():
    telemetry = Telemetry(limits=TelemetryLimits(components=1, clients=1))
    telemetry.register("fixture")
    with pytest.raises(ContractError):
        telemetry.register("other")
    telemetry.subscribe(BASE)
    with pytest.raises(ContractError):
        telemetry.subscribe(BASE)
    with pytest.raises(ContractError):
        telemetry.emit(BASE, "fixture", Metric.RUNNING, True)
    with pytest.raises(ContractError):
        telemetry.update_health("fixture", "ready")


def test_forged_trace_and_other_thread_cannot_mutate_owner():
    telemetry = Telemetry()
    telemetry.register("fixture")
    trace = telemetry.begin(BASE, "fixture")
    with pytest.raises(ContractError):
        telemetry.end(replace(trace), Outcome.SUCCEEDED)
    errors = []

    def mutate():
        try:
            telemetry.emit(BASE, "fixture", Metric.QUEUED, 1)
        except RuntimeError as error:
            errors.append(str(error))

    thread = Thread(target=mutate)
    thread.start()
    thread.join()
    assert errors == ["TELEMETRY_OWNER_THREAD_REQUIRED"]


def test_core_bootstrap_remains_closed_and_has_bounded_operational_services():
    from ai_agent_adapters.configuration import BootstrapSettings

    from ai_agent_core.bootstrap import create_app

    async def run():
        app = create_app(BootstrapSettings(memory_scope="test"))
        assert app.state.supervisor.snapshot.state == RunState.STOPPED
        assert isinstance(app.state.telemetry, Telemetry)
        assert app.state.scheduler.usage()
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.12)
            assert app.state.telemetry.health("resource_scheduler").state == HealthState.READY
            assert app.state.supervisor.snapshot.state == RunState.STOPPED
        assert app.state.supervisor.snapshot.state == RunState.STOPPED

    asyncio.run(run())


def test_bootstrap_respects_existing_worker_ram_and_pending_configuration():
    from ai_agent_adapters.configuration import BootstrapSettings

    from ai_agent_core.bootstrap import create_app

    app = create_app(BootstrapSettings(memory_scope="test", worker_memory_mib=512, pending_jobs=2))
    ram = next(item for item in app.state.scheduler.usage() if item.pool.id == "ram")
    assert ram.pool.capacity == 512
    assert app.state.scheduler.limits.pending == 2
    assert app.state.supervisor.snapshot.state == RunState.STOPPED
