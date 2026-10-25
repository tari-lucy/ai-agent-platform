"""Opt-in real empty prewarm, fresh context and late-result fences; no paid API."""

import asyncio
import json
import os
import time

import pytest
from ai_agent_adapters.agent_harness.opencode import PREFIX, OpenCodeHarness
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_adapters.sandbox.warm import WarmSandbox
from ai_agent_application.agent_workplace.ports import (
    HarnessTurn,
    ModelReply,
    RunnerLimits,
    RunnerTask,
)
from ai_agent_application.llm.tools import ToolCall
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from test_docker import World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit existing OpenCode image required"
)
CAP = RunnerLimits(60, 1024, 128, 60, 65536, 32 * 1024 * 1024)


@pytest.mark.parametrize("late", [None, "stop", "revoke"])
def test_empty_one_use_server_preserves_model_and_release_fences(
    monkeypatch, record_property, late
):
    async def scenario():
        monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_OPENCODE_IMAGE_ID"])
        w = World()
        template = RunnerTemplate(
            "python-task",
            os.environ["AW_OPENCODE_IMAGE_ID"],
            PREFIX,
            CAP,
            65536,
            model_role_id="activity",
            max_workspace_calls=2 if late is None else 0,
        )
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()

        class Model:
            async def generate(self, admission, request):
                calls.append(request)
                entered.set()
                if late is None and len(calls) == 1:
                    return ModelReply(
                        "completed",
                        "",
                        "fixture-tool",
                        tool_calls=(
                            ToolCall(
                                id="warm-tool",
                                name="platform_workspace",
                                arguments=json.dumps(
                                    {"operation": "capabilities", "arguments": {}}
                                ),
                            ),
                        ),
                    )
                if late is None:
                    assert any(
                        m.role == "tool" and "synthetic workspace marker" in m.content
                        for m in request.messages
                    )
                if late:
                    await release.wait()
                return ModelReply("completed", "Synthetic warm reply.", "fixture-attempt")

        class Workspace:
            async def call(self, admission, request):
                assert request.operation == "capabilities"
                return "synthetic workspace marker"

        def valid():
            if not w.alive:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        async def registered(name):
            await w.lifecycle(w.admission, name, "register")

        async def removed(name, success):
            assert success

        slot = WarmSandbox(
            template,
            workspace=late is None,
            helper=False,
            idle_seconds=30,
            valid=valid,
            registered=registered,
            released=removed,
        )
        runner = DockerTaskRunner(
            (template,),
            w,
            w,
            w,
            authorize=w.authorize,
            lifecycle=w.lifecycle,
            models=Model(),
            workspace=Workspace() if late is None else None,
            prepared=slot,
        )
        try:
            begin = time.monotonic()
            await slot.prepare()
            record_property("prewarm_seconds", round(time.monotonic() - begin, 3))
            assert not calls  # no model during prewarm; no context or files
            empty = await cli(
                "exec",
                slot.name,
                "/usr/local/bin/python",
                "-I",
                "-c",
                "import os; print(os.listdir('/work/inputs'))",
            )
            assert empty.strip() == b"[]"
            task = RunnerTask(
                template.id,
                (*PREFIX, "Synthetic fresh marker FRESH_WARM_A"),
                (),
                CAP,
                network="gateway",
            )
            assert slot.claim(template, task)
            assert not slot.claim(template, task)  # not a conversation cache
            harness = OpenCodeHarness(runner, template.id, CAP)
            begin = time.monotonic()
            turn = asyncio.create_task(
                harness.turn(
                    w.admission,
                    HarnessTurn("session", "Synthetic fresh marker FRESH_WARM_A", (), ()),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=30)
            metric = round(time.monotonic() - begin, 3)
            record_property("warm_to_model_seconds", metric)
            print("WARM_TO_MODEL_SECONDS", metric)
            if late == "stop":
                w.alive = False
            if late == "revoke":
                w.revoked = True
            release.set()
            if late == "stop":
                with pytest.raises(ContractError):
                    await asyncio.wait_for(turn, timeout=30)
                events = []
            else:
                events = await asyncio.wait_for(turn, timeout=30)
            if late is None:
                assert [(e.kind, e.detail) for e in events] == [("result", "Synthetic warm reply.")]
                assert "FRESH_WARM_A" in json.dumps([m.content for m in calls[0].messages])
                assert "FRESH_WARM_B" not in json.dumps([m.content for m in calls[0].messages])
            else:
                assert all(e.kind != "result" for e in events)
            assert len(calls) == (2 if late is None else 1) and slot.closed
            assert not (
                await cli("ps", "--all", "--filter", "name=" + slot.name, "--format", "{{.ID}}")
            ).strip()
        finally:
            release.set()
            await slot.close()
            await runner.purge()

    asyncio.run(scenario())
