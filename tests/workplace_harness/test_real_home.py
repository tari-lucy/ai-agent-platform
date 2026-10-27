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
    resident: WarmSandbox | None = None,
    cap: RunnerLimits = CAP,
) -> tuple[list, ScriptedProvider]:
    """One harness turn; ``warm`` prepares the sandbox first with that (older) guide.

    ``calls`` replaces the single bash step; ``egress`` gives the turn its gateway;
    ``resident`` is a kept sandbox claimed for this turn (it is not closed here).
    """
    w = World()
    template = replace(
        w.runner.templates["python-task"],
        image_id=os.environ["AW_HOME_IMAGE_ID"],
        executable_prefix=PREFIX,
        ceiling=cap,
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
    if resident is not None:
        task = RunnerTask(template.id, PREFIX, (), cap, network="gateway")
        assert resident.claim(template, task, home.volume, resident.internet)
    elif warm is not None:

        async def registered(name):
            await w.lifecycle(w.admission, name, "register")

        async def released(name, removed):
            assert removed

        slot = WarmSandbox(
            template, workspace=False, helper=False, idle_seconds=60, valid=lambda: None,
            registered=registered, released=released, home=warm,
        )  # fmt: skip
        await slot.prepare()
        task = RunnerTask(template.id, PREFIX, (), cap, network="gateway")
        assert not slot.claim(template, task, "aw-home-" + "0" * 32)  # another persona's home
        assert slot.claim(template, task, home.volume)
    runner = DockerTaskRunner(
        (template,), w, w, w, authorize=w.authorize, lifecycle=w.lifecycle,
        models=Models(), home=home, prepared=slot or resident, egress=egress,
    )  # fmt: skip
    try:
        events = await OpenCodeHarness(runner, template.id, cap).turn(
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


BACKGROUND = (
    'nohup python3 -c "import os, time, urllib.request as u; time.sleep(4); '
    "r = u.Request(os.environ['AI_WORKPLACE_MODEL_BASE_URL'] + '/chat/completions', "
    "data=b'{}', headers={'Content-Type': 'application/json'})\n"
    "try: u.urlopen(r, timeout=5); s = 'answered'\n"
    "except Exception as e: s = str(getattr(e, 'code', e))\n"
    "open('/workspace/bg.txt', 'w').write(s)\" >/dev/null 2>&1 &"
    " sleep 300 >/dev/null 2>&1 & echo $! > /workspace/sleeper.pid; echo resident-marker-one"
)


def test_resident_sandbox_keeps_processes_not_conversations(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])

    async def scenario():
        home = Home("aw-home-" + uuid4().hex, "Synthetic rule", "Synthetic facts", "Starter", 64)
        template = replace(
            World().runner.templates["python-task"],
            image_id=os.environ["AW_HOME_IMAGE_ID"],
            executable_prefix=PREFIX,
            ceiling=CAP,
            model_role_id="activity",
            max_model_calls=4,
            max_prompt_chars=100000,
            max_model_tokens=256,
        )
        receipts = []

        async def registered(name):
            pass

        async def released(name, removed):
            receipts.append(removed)

        slot = WarmSandbox(
            template, workspace=False, helper=False, idle_seconds=60, valid=lambda: None,
            registered=registered, released=released, home=home, resident=True,
            resident_idle_seconds=120,
        )  # fmt: skip
        try:
            await slot.prepare()
            events, _ = await turn(home, BACKGROUND, "First resident answer", resident=slot)
            assert [e.detail for e in events if e.kind == "result"] == ["First resident answer"]
            assert slot.turns == 1 and not slot.claimed and not slot.closed
            await asyncio.sleep(6)  # the background process asks the model between turns

            check = "kill -0 $(cat /workspace/sleeper.pid) && echo alive; cat /workspace/bg.txt"
            events, provider = await turn(home, check, "Second resident answer", resident=slot)
            assert [e.detail for e in events if e.kind == "result"] == ["Second resident answer"]
            output = tool_output(provider)
            assert "alive" in output and "403" in output, output
            # A fresh OpenCode session: nothing of the first turn's conversation is carried.
            first_turn = json.dumps([m.content for m in provider.calls[0].messages])
            assert (
                "First resident answer" not in first_turn
                and "resident-marker-one" not in first_turn
            )
            assert slot.turns == 2 and receipts == []
            # The platform process dies: the container ends with its background work.
            slot.process.kill()
            for _ in range(50):
                state = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", slot.name],
                    capture_output=True, text=True,
                ).stdout.strip()  # fmt: skip
                if state == "false":
                    break
                await asyncio.sleep(0.2)
            assert state == "false"
        finally:
            await slot.close()
            subprocess.run(
                ["docker", "volume", "rm", home.volume], capture_output=True, check=False
            )
        assert receipts == [True]

    asyncio.run(scenario())


@pytest.mark.skipif(not os.getenv("AW_EGRESS_LIVE"), reason="AW_EGRESS_LIVE=1 (real internet)")
def test_resident_sandbox_with_its_own_gateway(monkeypatch):
    from ai_agent_adapters.sandbox.docker import cli
    from ai_agent_adapters.sandbox.egress import Egress, EgressLimits

    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])

    async def scenario():
        rows = []
        image = os.environ["AW_HOME_IMAGE_ID"]
        home = Home("aw-home-" + uuid4().hex, "Synthetic rule", "Synthetic facts", "Starter", 64)
        template = replace(
            World().runner.templates["python-task"], image_id=image, executable_prefix=PREFIX,
            ceiling=CAP, model_role_id="activity", max_model_calls=4,
            max_prompt_chars=100000, max_model_tokens=256,
        )  # fmt: skip
        gateways = []

        def egress():
            gateways.append(Egress(image, EgressLimits(max_bytes=8 * 1024 * 1024), rows.append))
            return gateways[-1]

        async def nothing(*args):
            pass

        slot = WarmSandbox(
            template, workspace=False, helper=False, idle_seconds=60, valid=lambda: None,
            registered=nothing, released=nothing, home=home, resident=True,
            resident_idle_seconds=120, egress=egress,
        )  # fmt: skip
        try:
            await slot.prepare()
            task = RunnerTask(template.id, PREFIX, (), CAP, network="gateway")
            assert not slot.claim(template, task, home.volume, internet=False)
            for answer in ("First online answer", "Second online answer"):
                events, provider = await turn(home, FETCH, answer, resident=slot)
                assert [e.detail for e in events if e.kind == "result"] == [answer]
                assert "status 200" in tool_output(provider)
            assert len(gateways) == 1 and slot.turns == 2  # one gateway for both turns
            assert sum(r["target"] == "example.com:443" for r in rows) == 2
        finally:
            await slot.close()
            subprocess.run(
                ["docker", "volume", "rm", home.volume], capture_output=True, check=False
            )
        net = await cli(
            "network", "ls", "--filter", "name=" + gateways[0].network, "--format", "{{.ID}}"
        )
        proxy = await cli(
            "ps", "--all", "--filter", "name=" + gateways[0].name, "--format", "{{.ID}}"
        )
        assert not net.strip() and not proxy.strip()

    asyncio.run(scenario())


BURN = (
    'nohup python3 -c "import time\nt = time.process_time()\n'
    "while time.process_time() - t < 8: pass\n"
    "open('/workspace/burn.txt', 'w').write('burned')\" >/dev/null 2>&1 & echo started-burn"
)


def test_resident_cpu_rlimit_is_not_the_per_turn_limit(monkeypatch):
    """RLIMIT_CPU counts a process's life: in a resident sandbox the per-turn value would kill
    its background work and ``opencode serve`` after a few turns (Windows soak, 6th turn)."""
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])
    cap = replace(CAP, cpu_seconds=5)

    async def scenario():
        home = Home("aw-home-" + uuid4().hex, "Synthetic rule", "Synthetic facts", "Starter", 64)
        template = replace(
            World().runner.templates["python-task"],
            image_id=os.environ["AW_HOME_IMAGE_ID"],
            executable_prefix=PREFIX,
            ceiling=cap,
            model_role_id="activity",
            max_model_calls=4,
            max_prompt_chars=100000,
            max_model_tokens=256,
        )

        async def nothing(*args):
            pass

        slot = WarmSandbox(
            template, workspace=False, helper=False, idle_seconds=60, valid=lambda: None,
            registered=nothing, released=nothing, home=home, resident=True,
            resident_idle_seconds=120,
        )  # fmt: skip
        try:
            await slot.prepare()
            events, _ = await turn(home, BURN, "Burn started", resident=slot, cap=cap)
            assert [e.detail for e in events if e.kind == "result"] == ["Burn started"]
            await asyncio.sleep(14)  # 8 s of CPU under --cpus=1, past the 5 s turn value
            events, provider = await turn(
                home, "cat /workspace/burn.txt", "Checked", resident=slot, cap=cap
            )
            assert "burned" in tool_output(provider), tool_output(provider)
        finally:
            await slot.close()
            subprocess.run(
                ["docker", "volume", "rm", home.volume], capture_output=True, check=False
            )

    asyncio.run(scenario())
