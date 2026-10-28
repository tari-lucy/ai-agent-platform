"""The harness wakes owner and the wake_me tool (doc33 stage 3 port)."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import RunState
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp
from ai_agent_core.m3_composition.harness_wakes import HarnessWakes


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_a_wake_moves_new_pending_done_once_and_only_behind_the_guard(tmp_path):
    async def scenario():
        clock = Clock()
        wakes = HarnessWakes(tmp_path / "wakes.json", clock)
        wake = await wakes.set(10, "проверь сборку")
        assert await wakes.due() == []
        clock.now += timedelta(minutes=11)
        [due] = await wakes.due()
        assert due.id == wake.id and due.native_session_id is None
        assert not await wakes.reserve(wake.id, lambda: False)  # STOP between: nothing moves
        first, second = await asyncio.gather(
            wakes.reserve(wake.id, lambda: True), wakes.reserve(wake.id, lambda: True)
        )
        assert sorted((first, second)) == [False, True]
        assert [w.id for w in await wakes.pending()] == [wake.id]
        assert await wakes.recent_count(clock.now - timedelta(hours=1)) == 1
        assert await wakes.delivered(wake.id, lambda: True)
        assert not await wakes.delivered(wake.id, lambda: True)
        assert await wakes.due() == [] and await wakes.pending() == []
        reread = HarnessWakes(tmp_path / "wakes.json", clock)
        assert [w.state for w in reread._load()] == ["done"]

    asyncio.run(scenario())


def test_bounds_refuse(tmp_path):
    async def scenario():
        wakes = HarnessWakes(tmp_path / "wakes.json")
        for minutes, text in ((0, "x"), (10081, "x"), (5, "  "), (5, "x" * 2001)):
            with pytest.raises(ContractError):
                await wakes.set(minutes, text)
        for _ in range(20):
            await wakes.set(5, "x")
        with pytest.raises(ContractError):
            await wakes.set(5, "x")

    asyncio.run(scenario())


def test_wake_me_sets_a_wake_through_the_platform_mcp(tmp_path):
    async def scenario():
        snapshot = SimpleNamespace(state=RunState.ACTIVE, control_epoch=2, scope=None)
        rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)
        wakes = HarnessWakes(tmp_path / "wakes.json")
        mcp = PlatformMcp(rt, lambda: 2, wakes)  # type: ignore[arg-type]
        reply = await mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "wake_me", "arguments": {"minutes": 30, "text": "дальше"}},
            }
        )
        assert reply["result"]["isError"] is False  # type: ignore[index]
        [wake] = wakes._load()
        assert wake.text == "дальше" and wake.state == "new"

    asyncio.run(scenario())
