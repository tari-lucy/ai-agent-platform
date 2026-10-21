"""The pinned harness returns terminal responses, not fragments/errors/usage authority."""

import asyncio
import json

import pytest
from ai_agent_adapters.agent_harness import OpenCodeHarness
from ai_agent_adapters.agent_harness.worker import configuration, parse_events
from ai_agent_application.agent_workplace.ports import HarnessTurn, RunnerOutcome
from ai_agent_contracts.codec import ContractError
from test_docker import CAP, World


def events(*rows):
    return b"\n".join(json.dumps(row).encode() for row in rows)


def text(identifier, value):
    return {"type": "text", "part": {"messageID": identifier, "text": value}}


def end(identifier, reason="stop"):
    return {"type": "step_finish", "part": {"messageID": identifier, "reason": reason}}


def test_only_final_message_is_released_and_errors_are_fixed_codes():
    result = parse_events(
        events(text("a", "intermediate"), end("a", "tool-calls"), text("b", "final"), end("b")), 0
    )
    assert result == {"status": "completed", "text": "final", "tool_errors": []}
    for data, code in (
        (events(text("a", "partial")), 0),
        (events(text("a", "partial"), end("a", "length")), 0),
        (events(text("a", "finished"), end("a")), 1),
        (events({"type": "error", "error": "REMOTE_PRIVATE_ERROR"}), 0),
        (b"bad json", 0),
    ):
        refused = parse_events(data, code)
        assert refused["status"] == "failed" and "text" not in refused
        assert "REMOTE_PRIVATE_ERROR" not in str(refused)


def test_configuration_has_one_loopback_provider_no_background_agents_or_installable_plugins():
    config = configuration("http://127.0.0.1:1234/v1", 256)
    assert config["enabled_providers"] == ["platform"] and config["plugin"] == []
    assert not config["mcp"] and not config["lsp"] and not config["formatter"]
    assert config["provider"]["platform"]["models"]["bound"]["limit"]["output"] == 256
    assert config["agent"]["title"]["disable"] and config["agent"]["summary"]["disable"]
    assert config["agent"]["platform"]["permission"]["*"] == "deny"


def test_adapter_returns_staged_outputs_and_close_drops_runner_cache(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", "sha256:" + "a" * 64)
    world = World()

    class Runner:
        def __init__(self):
            self.calls, self.cancelled = [], []

        async def run(self, admission, task):
            self.calls.append(task)
            return RunnerOutcome(
                "succeeded",
                0,
                (),
                json.dumps({"status": "completed", "text": "synthetic", "tool_errors": []}),
            )

        async def cancel(self, admission):
            self.cancelled.append(admission)

    async def scenario():
        runner = Runner()
        harness = OpenCodeHarness(runner, "opencode", CAP)
        turn = HarnessTurn("session", "Synthetic instruction", (), ())
        result = await harness.turn(world.admission, turn)
        assert [(e.kind, e.detail) for e in result] == [("result", "synthetic")]
        assert runner.calls[0].network == "gateway"
        assert runner.calls[0].command[-1] == turn.instruction
        await harness.close("unrelated")
        assert not runner.cancelled
        await harness.close("session")
        assert runner.cancelled == [world.admission]
        world.alive = False
        with pytest.raises(ContractError):
            await harness.turn(world.admission, turn)
        assert len(runner.calls) == 1

    asyncio.run(scenario())
