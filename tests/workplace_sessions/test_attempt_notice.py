"""A rejected step leaves a visible bounded explanation; STOP still forbids updates."""

import asyncio
from types import SimpleNamespace

import pytest
from ai_agent_application.agent_activity.book import ActivityBook
from ai_agent_contracts.codec import ContractError
from test_sessions import ACT, AREA, world

from ai_agent_core.m3_composition.activity import AutonomousActivity


def test_terminal_notice_is_visible_idempotent_and_does_not_reopen_finished_task(tmp_path):
    async def scenario():
        w, _, _ = await world(tmp_path)
        try:
            owner = ActivityBook(w.store, w.clock, principal=ACT)
            task = await owner.open_task(
                "assignment", AREA, "assignment", "Synthetic goal", "test", limit=20
            )
            notices = []
            snapshot = SimpleNamespace(
                state=SimpleNamespace(value="ACTIVE"),
                activity=SimpleNamespace(value="OPEN"),
                scope=AREA,
            )
            runtime = SimpleNamespace(
                store=w.store,
                supervisor=SimpleNamespace(snapshot=snapshot),
                notify=lambda *args: notices.append(args),
            )
            ticker = AutonomousActivity(runtime)
            await ticker._attempt_notice(AREA, owner, task.id, 1, "cancelled")
            revised = await owner.tasks_repo(AREA).get(task.id)
            assert "Отменена" in revised.outcome and revised.state == task.state
            assert len(notices) == 1
            await ticker._attempt_notice(AREA, owner, task.id, 1, "cancelled")
            assert len(notices) == 1
            await ticker._attempt_notice(AREA, owner, task.id, 6, "limit")
            revised = await owner.tasks_repo(AREA).get(task.id)
            assert "предел попыток" in revised.outcome and "продолжения не будет" in revised.outcome
            runtime.supervisor.snapshot = SimpleNamespace(
                state=SimpleNamespace(value="STOPPED"),
                activity=SimpleNamespace(value="CLOSED"),
                scope=AREA,
            )
            with pytest.raises(ContractError):
                await ticker._attempt_notice(AREA, owner, task.id, 7, "unknown")
            assert (await owner.tasks_repo(AREA).get(task.id)).version == revised.version
            runtime.supervisor.snapshot = snapshot
            done = await owner.update_task(
                "done", revised, state="done", outcome="Synthetic complete"
            )
            await ticker._attempt_notice(AREA, owner, task.id, 7, "failed")
            assert (await owner.tasks_repo(AREA).get(task.id)).version == done.version
        finally:
            await w.close()

    asyncio.run(scenario())
