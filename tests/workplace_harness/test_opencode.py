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
    assert result == {
        "status": "completed",
        "text": "final",
        "tool_errors": [],
        "steps": [],
        "steps_omitted": 0,
    }
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


def tool(name, status, input_, output):
    key = "output" if status == "completed" else "error"
    state = {"status": status, "input": input_, key: output}
    return {"type": "tool_use", "part": {"type": "tool", "tool": name, "state": state}}


def test_finished_tool_calls_form_a_clipped_journal_kept_on_failure():
    big = "x" * 100000
    rows = events(
        tool("bash", "completed", {"command": "python3 parse.py"}, big),
        end("a", "tool-calls"),
        tool("helpers_run_script", "error", {"file_id": "s"}, "ACCESS_DENIED"),
        tool("bad name!", "completed", {}, ""),
        {
            "type": "tool_use",
            "part": {"type": "tool", "tool": "read", "state": {"status": "running"}},
        },
        text("b", "final"),
        end("b"),
    )
    result = parse_events(rows, 0)
    assert result["status"] == "completed" and result["tool_errors"] == ["tool"]
    first, second, third = result["steps"]
    assert first["tool"] == "bash" and first["input"] == '{"command": "python3 parse.py"}'
    assert len(first["output"]) == 400 and first["output_chars"] == 100000
    assert second == {
        "tool": "helpers_run_script",
        "status": "error",
        "input": '{"file_id": "s"}',
        "output": "ACCESS_DENIED",
        "output_chars": 13,
    }
    assert third["tool"] == "tool" and result["steps_omitted"] == 0
    failed = parse_events(rows, 1)
    assert failed["status"] == "failed" and len(failed["steps"]) == 3


def test_journal_keeps_latest_steps_within_its_budget():
    many = [tool("bash", "completed", {"n": i}, "é" * 1000) for i in range(60)]
    result = parse_events(events(*many, text("b", "done"), end("b")), 0)
    steps = result["steps"]
    assert steps[-1]["input"] == '{"n": 59}'
    assert len(json.dumps(steps, ensure_ascii=False).encode()) <= 16384
    assert len(steps) + result["steps_omitted"] == 60


def test_intermediate_narration_does_not_count_against_the_response():
    narration = [
        row for i in range(5) for row in (text(f"m{i}", "n" * 5000), end(f"m{i}", "tool-calls"))
    ]
    result = parse_events(events(*narration, text("z", "final"), end("z")), 0)
    assert result["status"] == "completed" and result["text"] == "final"


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


def test_adapter_passes_the_journal_and_refuses_a_malformed_one(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", "sha256:" + "a" * 64)
    world = World()
    step = {"tool": "bash", "status": "completed", "input": "{}", "output": "ok", "output_chars": 2}
    rows = [
        {
            "status": "completed",
            "text": "t",
            "tool_errors": [],
            "steps": [step],
            "steps_omitted": 2,
        },
        {"status": "failed", "code": "OPENCODE_FAILED", "steps": [step], "steps_omitted": 0},
        {"status": "completed", "text": "t", "tool_errors": [], "steps": [dict(step, x=1)]},
        {"status": "completed", "text": "t", "tool_errors": [], "steps_omitted": -1},
    ]

    class Runner:
        async def run(self, admission, task):
            return RunnerOutcome("succeeded", 0, (), json.dumps(rows.pop(0)))

        async def cancel(self, admission):
            pass

    async def scenario():
        harness = OpenCodeHarness(Runner(), "opencode", CAP)
        turn = HarnessTurn("session", "Synthetic instruction", (), ())
        (done,) = await harness.turn(world.admission, turn)
        assert done.kind == "result" and done.steps[0].output == "ok" and done.steps_omitted == 2
        (failed,) = await harness.turn(world.admission, turn)
        assert failed.kind == "failed" and len(failed.steps) == 1
        for _ in range(2):
            (refused,) = await harness.turn(world.admission, turn)
            assert (refused.kind, refused.detail) == ("failed", "OPENCODE_RESULT_INVALID")

    asyncio.run(scenario())
