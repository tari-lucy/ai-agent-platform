"""A wake is a coalesced scheduling hint, never a command or START."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from test_sessions import AREA

from ai_agent_core.m3_composition.service import Runtime


def test_arrival_wakes_the_parked_runtime_and_is_not_lost_before_wait():
    async def scenario():
        rt = Runtime.__new__(Runtime)
        rt._work_ready = asyncio.Event()
        waiting = asyncio.create_task(rt.wait_for_work(60))
        await asyncio.sleep(0)
        rt.wake()
        await asyncio.wait_for(waiting, timeout=1)
        rt.wake()
        rt.wake()  # bursts collapse, they are not commands queued for execution
        await asyncio.wait_for(rt.wait_for_work(60), timeout=1)
        assert not rt._work_ready.is_set()
        await rt.wait_for_work(0.001)  # periodic fallback still works with no event

    asyncio.run(scenario())


def test_only_operator_activity_hint_wakes_not_the_agents_own_notification():
    async def scenario():
        rt = Runtime.__new__(Runtime)
        rt._work_ready = asyncio.Event()
        rt.supervisor = SimpleNamespace(
            clock=SimpleNamespace(utc=lambda: datetime.now(UTC)),
            snapshot=SimpleNamespace(scope=AREA, state="STOPPED"),
        )
        rt.autonomous_activity = SimpleNamespace(next_check=100)
        events = []
        rt.events = SimpleNamespace(publish=events.append)
        rt.notify("ticket.changed", AREA, "fixture-ticket", 2)
        assert not rt._work_ready.is_set()  # agent-generated events must not spin the tick
        rt.wake_activity(AREA.model_copy(update={"persona_id": "other"}))
        assert not rt._work_ready.is_set()
        rt.wake_activity(AREA)
        assert rt._work_ready.is_set() and rt.autonomous_activity.next_check == 0
        assert rt.supervisor.snapshot.state == "STOPPED"
        assert len(events) == 1  # no job, Permission or model route exists in this fixture

    asyncio.run(scenario())


def test_start_reopens_scheduler_without_a_thirty_second_stopped_throttle():
    async def scenario():
        from ai_agent_application.agent_activity.model import ActivitySettings
        from ai_agent_contracts.states import ActivityState, RunState

        from ai_agent_core.m3_composition.activity import AutonomousActivity

        run = SimpleNamespace(
            scope=AREA,
            state=RunState.STOPPED,
            activity=ActivityState.OPEN,
            autonomy="autonomous",
            control_epoch=1,
            run_id="fixture",
        )
        rt = Runtime.__new__(Runtime)
        rt._work_ready = asyncio.Event()
        rt._activity_key = None
        clock_ticks = []
        rt.clock = SimpleNamespace(observe=lambda run, elapsed: clock_ticks.append(elapsed))
        rt.supervisor = SimpleNamespace(snapshot=run, clock=SimpleNamespace(elapsed=lambda: 0.0))
        rt.private_api_budget = SimpleNamespace(activity_enabled=True)
        rt.activity = ActivitySettings()  # production check_seconds=30, no test fast()
        rt.tickets = object()
        rt.autonomous_activity = AutonomousActivity(rt)
        rt._observe_run(run, 0.0)
        assert await rt.autonomous_activity.tick() == "RUN_NOT_AUTONOMOUS"
        assert rt.autonomous_activity.next_check == 0
        rt.autonomous_activity.next_check = 30  # a previous active run's wait must not delay START

        class NoReentrantSnapshot:
            @property
            def snapshot(self):
                raise AssertionError("observer must not reenter Supervisor.poll")

        rt.supervisor = NoReentrantSnapshot()
        run.state = RunState.ACTIVE
        rt._observe_run(run, 1.0)
        assert rt.autonomous_activity.next_check == 0 and rt._work_ready.is_set()
        rt._work_ready.clear()
        rt._observe_run(run, 2.0)  # checkpoint/notification at same state is not another wake
        assert not rt._work_ready.is_set()
        assert clock_ticks == [0.0, 1.0, 2.0]

    asyncio.run(scenario())
