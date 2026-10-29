"""The platform MCP boundary (doc33 stage 2): protocol, tools, refusal outside the epoch."""

import asyncio
from types import SimpleNamespace

from ai_agent_contracts.states import RunState

from ai_agent_core.m3_composition.harness_mcp import PlatformMcp


def runtime(state: RunState = RunState.ACTIVE, epoch: int = 3) -> SimpleNamespace:
    snapshot = SimpleNamespace(state=state, control_epoch=epoch, scope=None)
    return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)


def call(mcp: PlatformMcp, body: object) -> object:
    return asyncio.run(mcp.handle(body))


def test_initialize_list_and_notifications():
    mcp = PlatformMcp(runtime(), lambda: 3)  # type: ignore[arg-type]
    init = call(mcp, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}  # type: ignore[index]
    tools = call(mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [tool["name"] for tool in tools["result"]["tools"]]  # type: ignore[index]
    assert names == ["persona_brief", "memory_recall", "ticket_raise", "wake_me", "tickets"]
    assert call(mcp, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    missing = call(mcp, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert missing["error"]["code"] == -32601  # type: ignore[index]


def test_a_call_after_stop_or_in_another_epoch_is_refused_without_reading():
    for rt, epoch in ((runtime(RunState.STOPPING), 3), (runtime(epoch=4), 3)):
        mcp = PlatformMcp(rt, lambda epoch=epoch: epoch)  # type: ignore[arg-type]
        reply = call(
            mcp,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "memory_recall", "arguments": {"query": "x"}},
            },
        )
        result = reply["result"]  # type: ignore[index]
        assert result["isError"] is True
        assert result["content"][0]["text"] == "Отказ: STALE_CONTEXT"


def test_tickets_without_the_owner_are_unavailable():
    mcp = PlatformMcp(runtime(), lambda: 3)  # type: ignore[arg-type]
    reply = call(
        mcp,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "ticket_raise", "arguments": {"title": "t", "body": "b"}},
        },
    )
    assert reply["result"]["content"][0]["text"] == "Отказ: CAPABILITY_UNAVAILABLE"  # type: ignore[index]
