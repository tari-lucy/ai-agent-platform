"""Opt-in real OpenCode/Windows Docker Desktop path, all content and provider synthetic."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from ai_agent_adapters.agent_harness import OpenCodeHarness
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_application.agent_workplace.ports import HarnessTurn, ModelReply, RunnerLimits
from ai_agent_application.llm import (
    Delta,
    Finished,
    GenerationParameters,
    GenerationRequest,
    LLMGateway,
    ResolvedAssignment,
    ToolCallDelta,
    UsageReported,
)
from ai_agent_contracts.llm import TokenUsage
from test_docker import World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit verified OpenCode image required"
)


def test_real_opencode_reads_staged_input_and_returns_via_existing_gateway(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_OPENCODE_IMAGE_ID"])

    async def scenario():
        w = World()
        cap = RunnerLimits(15, 512, 128, 30, 65536, 32 * 1024 * 1024)
        template = replace(
            w.runner.templates["python-task"],
            executable_prefix=PREFIX,
            ceiling=cap,
            model_role_id="activity",
            max_model_calls=2,
            max_prompt_chars=100000,
            max_model_tokens=256,
        )
        w.runner.templates[template.id] = template
        usage = TokenUsage(
            input_tokens=12, output_tokens=8, source="provider", completeness="complete"
        )
        provider = ScriptedProvider(
            [
                [
                    ToolCallDelta(0, "read_a", "read", '{"filePath":"/work/inputs/manifest.json"}'),
                    Finished("tool_calls"),
                    UsageReported(usage),
                ],
                [Delta("Synthetic final answer"), Finished("stop"), UsageReported(usage)],
            ]
        )
        reports = []
        binding = ResolvedAssignment(
            "assignment",
            1,
            "activity",
            "connection",
            "mock",
            "model",
            "ENDPOINT_NOT_READ",
            "KEY_NOT_READ",
            GenerationParameters(max_output_tokens=512),
            "unverified",
        )
        gateway = LLMGateway(lambda _: provider, lambda _: binding, sink=reports.append)

        class Models:
            async def generate(self, admission, request):
                assert admission == w.admission
                generated = await gateway.generate(
                    GenerationRequest(
                        operation_id=request.call_id,
                        role_id=request.role_id,
                        messages=request.messages,
                        max_output_tokens=request.max_output_tokens,
                        tools=request.tools,
                    )
                )
                return ModelReply(
                    generated.status,
                    generated.text or "",
                    "synthetic-attempt",
                    tool_calls=generated.tool_calls,
                )

        w.runner.models = Models()
        harness = OpenCodeHarness(w.runner, template.id, cap)
        result = await harness.turn(
            w.admission,
            HarnessTurn(
                "synthetic-session", "Read the staged manifest. Return a synthetic answer.", (), ()
            ),
        )
        assert [(e.kind, e.detail) for e in result] == [("result", "Synthetic final answer")]
        assert len(provider.calls) == len(reports) == 2  # no title/summary calls
        assert all(call.assignment.parameters.max_output_tokens == 256 for call in provider.calls)
        assert all(report.usage.input_tokens == 12 for report in reports)
        history = provider.calls[1].messages
        tool = next(message for message in history if message.role == "tool")
        assert tool.tool_call_id == "read_a" and "[]" in tool.content
        assert "prevents you" not in tool.content
        assert json.loads(history[-2].tool_calls[0].arguments)["filePath"].endswith("manifest.json")
        assert w.names[-1][1] == "removed"
        await harness.close("synthetic-session")
        assert w.runner._cache is None

    asyncio.run(scenario())
