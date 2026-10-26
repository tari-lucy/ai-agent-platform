"""Opt-in real OpenCode with a W2 home: bash writes the volume, the next container reads it.

All content and the provider are synthetic; the test volume is removed at the end.
"""

import asyncio
import json
import os
import subprocess
from dataclasses import replace
from uuid import uuid4

import pytest
from ai_agent_adapters.agent_harness import OpenCodeHarness
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.policy import Home
from ai_agent_adapters.sandbox.warm import WarmSandbox
from ai_agent_application.agent_workplace.ports import (
    HarnessTurn,
    ModelReply,
    RunnerLimits,
    RunnerTask,
)
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
    not os.getenv("AW_HOME_IMAGE_ID"), reason="explicit workspace-capable OpenCode image required"
)
CAP = RunnerLimits(30, 768, 128, 60, 65536, 32 * 1024 * 1024)
USAGE = TokenUsage(input_tokens=12, output_tokens=8, source="provider", completeness="complete")


def bash(call: str, command: str) -> list[object]:
    arguments = json.dumps({"command": command, "description": "Synthetic step"})
    return [ToolCallDelta(0, call, "bash", arguments), Finished("tool_calls"), UsageReported(USAGE)]


async def turn(
    home: Home,
    command: str,
    answer: str,
    warm: Home | None = None,
    *,
    calls: list[list[object]] | None = None,
    egress=None,
) -> tuple[list, ScriptedProvider]:
    """One harness turn; ``warm`` prepares the sandbox first with that (older) guide.

    ``calls`` replaces the single bash step; ``egress`` gives the turn its gateway.
    """
    w = World()
    template = replace(
        w.runner.templates["python-task"],
        image_id=os.environ["AW_HOME_IMAGE_ID"],
        executable_prefix=PREFIX,
        ceiling=CAP,
        model_role_id="activity",
        max_model_calls=4,
        max_prompt_chars=100000,
        max_model_tokens=256,
    )
    provider = ScriptedProvider(
        [
            *(calls or [bash("bash_a", command)]),
            [Delta(answer), Finished("stop"), UsageReported(USAGE)],
        ]
    )
    binding = ResolvedAssignment(
        "assignment", 1, "activity", "connection", "mock", "model",
        "ENDPOINT_NOT_READ", "KEY_NOT_READ", GenerationParameters(max_output_tokens=512),
        "unverified",
    )  # fmt: skip
    gateway = LLMGateway(lambda _: provider, lambda _: binding)

    class Models:
        async def generate(self, admission, request):
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
                generated.status, generated.text or "", "synthetic", tool_calls=generated.tool_calls
            )

    slot = None
    if warm is not None:

        async def registered(name):
            await w.lifecycle(w.admission, name, "register")

        async def released(name, removed):
            assert removed

        slot = WarmSandbox(
            template, workspace=False, helper=False, idle_seconds=60, valid=lambda: None,
            registered=registered, released=released, home=warm,
        )  # fmt: skip
        await slot.prepare()
        task = RunnerTask(template.id, PREFIX, (), CAP, network="gateway")
        assert not slot.claim(template, task, "aw-home-" + "0" * 32)  # another persona's home
        assert slot.claim(template, task, home.volume)
    runner = DockerTaskRunner(
        (template,), w, w, w, authorize=w.authorize, lifecycle=w.lifecycle,
        models=Models(), home=home, prepared=slot, egress=egress,
    )  # fmt: skip
    try:
        events = await OpenCodeHarness(runner, template.id, CAP).turn(
            w.admission, HarnessTurn("synthetic-session", "Synthetic instruction.", (), ())
        )
    finally:
        if slot is not None:
            await slot.close()
    assert w.names[-1][1] == "removed"
    return events, provider


def tool_output(provider: ScriptedProvider) -> str:
    history = provider.calls[1].messages
    return next(message for message in history if message.role == "tool").content


def test_real_opencode_shell_keeps_files_in_the_home(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])

    async def scenario():
        home = Home(
            "aw-home-" + uuid4().hex, "Synthetic operator rule", "Synthetic facts", "Starter", 64
        )
        try:
            events, provider = await turn(
                home,
                "mkdir -p /workspace/projects/s && echo kept-marker > /workspace/projects/s/n.txt"
                " && touch /planted; echo exit=$?",
                "First synthetic answer",
            )
            (result,) = [e for e in events if e.kind == "result"]
            assert result.detail == "First synthetic answer"
            assert result.steps[0].tool == "bash" and result.steps[0].status == "completed"
            assert "exit=1" in tool_output(provider)  # the read-only root refused /planted
            system = provider.calls[0].messages[0].content
            assert "Instructions from: /guide/AGENTS.md" in system
            assert "Synthetic operator rule" in system and "Starter" in system

            events, provider = await turn(
                home, "cat /workspace/projects/s/n.txt", "Second synthetic answer"
            )
            assert [e.detail for e in events if e.kind == "result"] == ["Second synthetic answer"]
            assert "kept-marker" in tool_output(provider)

            # Prewarmed: the volume is mounted at preparation, the guide is this turn's copy.
            fresh = replace(home, rules="Fresh operator rule")
            events, provider = await turn(
                fresh,
                "cat /guide/AGENTS.md /workspace/projects/s/n.txt",
                "Warm synthetic answer",
                warm=home,
            )
            assert [e.detail for e in events if e.kind == "result"] == ["Warm synthetic answer"]
            output = tool_output(provider)
            assert "Fresh operator rule" in output and "kept-marker" in output
            assert "Synthetic operator rule" not in output
        finally:
            subprocess.run(
                ["docker", "volume", "rm", home.volume], capture_output=True, check=False
            )

    asyncio.run(scenario())


FETCH = (
    'python3 -c "import urllib.request as u; '
    "print('status', u.urlopen('https://example.com', timeout=20).status)\""
)


@pytest.mark.skipif(not os.getenv("AW_EGRESS_LIVE"), reason="AW_EGRESS_LIVE=1 (real internet)")
def test_real_opencode_reaches_the_internet_through_the_gateway(monkeypatch):
    from ai_agent_adapters.sandbox.egress import Egress, EgressLimits

    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])

    async def scenario():
        rows = []
        home = Home("aw-home-" + uuid4().hex, "Synthetic rule", "Synthetic facts", "Starter", 64)
        fetch = json.dumps({"url": "https://example.com", "format": "text"})
        calls = [
            bash("bash_a", FETCH),
            [ToolCallDelta(0, "web_a", "webfetch", fetch), Finished("tool_calls"),
             UsageReported(USAGE)],
        ]  # fmt: skip
        image = os.environ["AW_HOME_IMAGE_ID"]
        try:
            events, provider = await turn(
                home, "", "Internet synthetic answer", calls=calls,
                egress=lambda: Egress(image, EgressLimits(max_bytes=8 * 1024 * 1024), rows.append),
            )  # fmt: skip
            (result,) = [e for e in events if e.kind == "result"]
            assert [(s.tool, s.status) for s in result.steps] == [
                ("bash", "completed"),
                ("webfetch", "completed"),
            ], result.steps
            tools = [m.content for m in provider.calls[2].messages if m.role == "tool"]
            assert "status 200" in tools[0] and "Example Domain" in tools[1]
            fetched = [r for r in rows if r["target"] == "example.com:443"]
            assert len(fetched) == 2 and all(r["status"] == "ok" for r in fetched)
            assert all(r["method"] == "CONNECT" for r in rows)  # HTTPS: host:port only
        finally:
            subprocess.run(
                ["docker", "volume", "rm", home.volume], capture_output=True, check=False
            )

    asyncio.run(scenario())
