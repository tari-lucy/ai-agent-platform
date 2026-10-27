"""Why a run faults is logged: fixed code, code location and the blocking site (no data).

Synthetic host, controlled delay: a tick step blocks the event loop synchronously past the
supervisor's sleep-gap limit (lowered for the test). STOP and limits are not relaxed.
"""

import asyncio
import logging
import time
from dataclasses import replace

from ai_agent_contracts.states import ErrorCode, RunState
from quality_support import Model, prepared_host


def blocking_step_for_the_test(seconds: float) -> None:
    time.sleep(seconds)  # synchronous work on the event loop thread, as in the report


def test_a_blocked_event_loop_faults_the_run_and_says_where(tmp_path, monkeypatch, caplog):
    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        async with host.open():
            rt = host.env.runtime
            await host.start()
            assert rt.supervisor.snapshot.state is RunState.ACTIVE
            rt.supervisor.limits = replace(rt.supervisor.limits, sleep_gap_seconds=1.0)
            original, blocked = rt.daily_memory.tick, []

            async def slow_tick():
                if not blocked:
                    blocked.append(True)
                    blocking_step_for_the_test(1.8)
                await original()

            monkeypatch.setattr(rt.daily_memory, "tick", slow_tick)
            with caplog.at_level(logging.WARNING):
                rt.wake()
                for _ in range(100):
                    if rt.supervisor.snapshot.state is RunState.FAULTED:
                        break
                    await asyncio.sleep(0.05)
            assert rt.supervisor.snapshot.state is RunState.FAULTED
            assert rt.supervisor.last_error is ErrorCode.STALE_CONTEXT

    asyncio.run(scenario())
    text = caplog.text
    assert "Event loop blocked" in text and "blocking_step_for_the_test" in text
    assert "Run clock gap: monotonic=" in text and "limit=1.0s" in text
    assert "Run faulted: STALE_CONTEXT at supervisor.py:poll" in text


def test_an_exception_in_the_runtime_tick_is_logged_before_the_fault(tmp_path, monkeypatch, caplog):
    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        async with host.open():
            rt = host.env.runtime
            await host.start()

            async def broken_tick_for_the_test():
                raise RuntimeError("synthetic secret text that must not be logged")

            monkeypatch.setattr(rt.daily_memory, "tick", broken_tick_for_the_test)
            with caplog.at_level(logging.WARNING):
                rt.wake()
                for _ in range(100):
                    if rt.supervisor.snapshot.state is RunState.FAULTED:
                        break
                    await asyncio.sleep(0.05)
            assert rt.supervisor.snapshot.state is RunState.FAULTED

    asyncio.run(scenario())
    text = caplog.text
    assert "Runtime tick failed: RuntimeError - at" in text
    assert "broken_tick_for_the_test" in text and "service.py:tick" in text
    assert "Run faulted: SERVICE_UNAVAILABLE at bootstrap.py:runtime_watchdog" in text
    assert "synthetic secret text" not in text  # the message is never logged


def test_stop_during_the_heartbeat_refresh_stops_without_a_fault(tmp_path, monkeypatch):
    """The double-STOP of the personal host: a STOP landing between the tick's state check and
    its heartbeat refresh used to fault the already stopping run."""
    from ai_agent_contracts.codec import ContractError

    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        async with host.open():
            rt = host.env.runtime
            await host.start()
            supervisor, raced = rt.supervisor, []
            original = supervisor.refresh

            async def refresh_racing_a_stop(admission, **kwargs):
                if not raced:
                    raced.append(True)
                    run = supervisor.snapshot
                    response = await host.env.client.post(
                        f"/api/v1/agents/{run.agent_id}/stop",
                        json=dict(
                            type="agent.stop",
                            request_id="race-stop",
                            target=run.agent_id,
                            scope=run.scope.model_dump(mode="json"),
                        ),
                        headers=host.env.headers,
                    )
                    assert response.status_code == 200, response.text
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                return await original(admission, **kwargs)

            monkeypatch.setattr(supervisor, "refresh", refresh_racing_a_stop)
            rt._refresh_at = 0
            rt.wake()
            for _ in range(100):
                if raced and supervisor.snapshot.state is RunState.STOPPED:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)  # a few more ticks: no fault appears afterwards
            assert raced and supervisor.snapshot.state is RunState.STOPPED

    asyncio.run(scenario())
