"""Bounded wake dispatch on a fake native API; no model or container launch."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope

from ai_agent_core.m3_composition.harness_autonomy import DueWake, HarnessAutonomy, WakeRun
from ai_agent_core.m3_composition.work_session_wake import WakeSettings

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")


class Owner:
    def __init__(self):
        self.state, self.count = "new", 0
        self.wake = DueWake("timer-a", "ses_a", NOW, "Проверить вымышленный результат.")

    async def due(self, scope, now):
        return self.wake

    async def recent_count(self, scope, now):
        return self.count

    async def reserve(self, scope, wake, message_id, guard):
        guard()
        state = self.state
        if state == "new":
            self.state = "pending"
        return state

    async def delivered(self, scope, wake, message_id, guard):
        guard()
        self.state = "done"


class Host:
    def __init__(self):
        self.calls, self.rows = [], []
        self.fail = False
        self.after_post = lambda: None

    async def opencode(self, method, path, body=None, *, timeout=60):
        self.calls.append(method)
        if method == "GET":
            return self.rows
        self.rows.append({"info": {"id": body["messageID"], "sessionID": "ses_a", "role": "user"}})
        self.after_post()
        if self.fail:
            raise TimeoutError
        return {"info": {"role": "assistant"}}


def build():
    host, owner = Host(), Owner()
    run = [WakeRun(SCOPE, 1, True, True, True, True)]
    service = HarnessAutonomy(
        host, owner, lambda: run[0], lambda s, e: None, lambda: NOW, lambda: 1, WakeSettings()
    )
    return service, host, owner, run


def test_uncertain_post_reconciles_without_repeating_the_paid_turn():
    async def scenario():
        service, host, owner, _ = build()
        host.fail = True
        with pytest.raises(TimeoutError):
            await service.tick()
        assert owner.state == "pending"
        assert await service.tick() == "FIRED"
        assert host.calls == ["POST", "GET"]
        assert await service.tick() == "ALREADY_DELIVERED"
        owner.state, host.rows = "pending", []
        assert await service.tick() == "OUTCOME_UNKNOWN"
        assert host.calls.count("POST") == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"active": False}, "RUN_NOT_AUTONOMOUS"),
        ({"ready": False}, "RUN_NOT_AUTONOMOUS"),
        ({"autonomous": False}, "RUN_NOT_AUTONOMOUS"),
        ({"scope": SCOPE.model_copy(update={"memory_mode": "test"})}, "TEST_SCOPE"),
    ],
)
def test_no_send_after_restart_or_without_autonomy(change, expected):
    service, host, owner, run = build()
    run[0] = replace(run[0], **change)
    assert asyncio.run(service.tick()) == expected
    assert host.calls == [] and owner.state == "new"


def test_budget_quiet_hours_hourly_cap_and_stop_during_post():
    async def scenario():
        service, host, owner, run = build()
        service.remaining = lambda: 0
        assert await service.tick() == "MONEY_BUDGET"
        service.remaining = lambda: 1
        hour = NOW.astimezone().hour
        service.settings = WakeSettings(quiet=(hour, (hour + 1) % 24))
        assert await service.tick() == "QUIET_HOURS"
        service.settings, owner.count = WakeSettings(), 4
        assert await service.tick() == "HOURLY_CAP"
        assert host.calls == []
        owner.count = 0

        def stop():
            run[0] = replace(run[0], active=False, epoch=2)

        host.after_post = stop
        with pytest.raises(ContractError):
            await service.tick()
        assert owner.state == "pending"
        assert await service.tick() == "RUN_NOT_AUTONOMOUS"
        await service.close()
        assert await service.tick() == "CLOSED_OR_BUSY"

    asyncio.run(scenario())
