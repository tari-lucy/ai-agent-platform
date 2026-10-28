"""The harness wakes owner and the wake_me tool (doc33 stage 3 port)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import RunState

from ai_agent_core.m3_composition.harness_mcp import PlatformMcp
from ai_agent_core.m3_composition.harness_wakes import HarnessWakes

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_a_wake_moves_new_pending_done_once_and_only_behind_the_guard(tmp_path):
    async def scenario():
        clock = Clock()
        wakes = HarnessWakes(tmp_path / "wakes.json", clock)
        wake = await wakes.set(10, "проверь сборку", scope=SCOPE, guard=lambda: True)
        assert await wakes.due(scope=SCOPE) == []
        clock.now += timedelta(minutes=11)
        [due] = await wakes.due(scope=SCOPE)
        assert due.id == wake.id and due.native_session_id is None

        async def reserve(guard):
            return await wakes.reserve(
                wake.id, guard, scope=SCOPE, native_session_id="ses_a", message_id="msg_a"
            )

        assert not await reserve(lambda: False)  # STOP between: nothing moves
        first, second = await asyncio.gather(reserve(lambda: True), reserve(lambda: True))
        assert sorted((first, second)) == [False, True]
        assert [w.id for w in await wakes.pending(scope=SCOPE)] == [wake.id]
        assert await wakes.recent_count(clock.now - timedelta(hours=1), scope=SCOPE) == 1
        assert await wakes.delivered(wake.id, lambda: True, scope=SCOPE)
        assert not await wakes.delivered(wake.id, lambda: True, scope=SCOPE)
        assert await wakes.due(scope=SCOPE) == [] and await wakes.pending(scope=SCOPE) == []
        reread = HarnessWakes(tmp_path / "wakes.json", clock)
        assert [w.state for w in reread._load()] == ["done"]

    asyncio.run(scenario())


def test_bounds_refuse(tmp_path):
    async def scenario():
        wakes = HarnessWakes(tmp_path / "wakes.json")
        for minutes, text in ((0, "x"), (10081, "x"), (5, "  "), (5, "x" * 2001)):
            with pytest.raises(ContractError):
                await wakes.set(minutes, text, scope=SCOPE, guard=lambda: True)
        for _ in range(20):
            await wakes.set(5, "x", scope=SCOPE, guard=lambda: True)
        with pytest.raises(ContractError):
            await wakes.set(5, "x", scope=SCOPE, guard=lambda: True)

    asyncio.run(scenario())


def test_wake_me_sets_a_wake_through_the_platform_mcp(tmp_path):
    async def scenario():
        snapshot = SimpleNamespace(state=RunState.ACTIVE, control_epoch=2, scope=SCOPE)
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


def test_partition_isolation_pinned_target_and_stop_while_waiting_for_lock(tmp_path):
    async def scenario():
        clock = Clock()
        path = tmp_path / "wakes.json"
        wakes = HarnessWakes(path, clock)
        wake = await wakes.set(1, "только для A", scope=SCOPE, guard=lambda: True)
        clock.now += timedelta(minutes=2)
        other = SCOPE.model_copy(update={"persona_id": "other"})
        test_scope = SCOPE.model_copy(update={"memory_mode": "test"})
        for scope in (other, test_scope):
            assert await wakes.due(scope=scope) == []
            assert not await wakes.reserve(
                wake.id, lambda: True, scope=scope, native_session_id="ses_b", message_id="msg_b"
            )
        assert await wakes.reserve(
            wake.id, lambda: True, scope=SCOPE, native_session_id="ses_a", message_id="msg_a"
        )
        wakes = HarnessWakes(path, clock)
        [pending] = await wakes.pending(scope=SCOPE)
        assert (pending.native_session_id, pending.message_id) == ("ses_a", "msg_a")
        assert not await wakes.reserve(
            wake.id, lambda: True, scope=SCOPE, native_session_id="ses_b", message_id="msg_b"
        )
        assert (await wakes.get(wake.id, scope=SCOPE)) == pending
        before = path.read_bytes()
        active = True
        await wakes.lock.acquire()
        attempt = asyncio.create_task(
            wakes.set(1, "после STOP нельзя", scope=SCOPE, guard=lambda: active)
        )
        await asyncio.sleep(0)
        active = False
        wakes.lock.release()
        with pytest.raises(ContractError):
            await attempt
        assert path.read_bytes() == before

    asyncio.run(scenario())


def test_old_mcp_call_cannot_set_a_wake_after_stop_start_in_same_partition(tmp_path):
    async def scenario():
        snapshot = SimpleNamespace(state=RunState.ACTIVE, control_epoch=2, scope=SCOPE)
        rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)
        path = tmp_path / "wakes.json"
        wakes = HarnessWakes(path)
        mcp = PlatformMcp(rt, lambda: snapshot.control_epoch, wakes)
        await wakes.lock.acquire()
        task = asyncio.create_task(
            mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "wake_me",
                        "arguments": {"minutes": 1, "text": "старый вызов"},
                    },
                }
            )
        )
        await asyncio.sleep(0)
        snapshot.control_epoch = 3  # same scope, fresh START; host's dynamic epoch also changed
        wakes.lock.release()
        reply = await task
        assert reply["result"]["isError"] is True
        assert "STALE_CONTEXT" in reply["result"]["content"][0]["text"]
        assert not path.exists()

    asyncio.run(scenario())


def test_oversized_save_and_failed_flush_keep_the_previous_owner_state(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "wakes.json"
        wakes = HarnessWakes(path)
        wake = await wakes.set(1, "исходное", scope=SCOPE, guard=lambda: True)
        previous = path.read_bytes()
        with pytest.raises(ContractError):
            wakes._save([replace(wake, id=f"wake-{i}", text="ж" * 2000) for i in range(1100)])
        assert path.read_bytes() == previous

        def failed_flush(fd):
            raise OSError("synthetic flush failure")

        monkeypatch.setattr("ai_agent_core.m3_composition.harness_learning.os.fsync", failed_flush)
        with pytest.raises(OSError):
            await wakes.reserve(
                wake.id, lambda: True, scope=SCOPE, native_session_id="ses_a", message_id="msg_a"
            )
        assert path.read_bytes() == previous
        assert (await wakes.get(wake.id, scope=SCOPE)).state == "new"

    asyncio.run(scenario())
