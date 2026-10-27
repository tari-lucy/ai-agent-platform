"""Explicit hour-long Desktop resident soak; real OpenCode, scripted model, synthetic home."""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from uuid import uuid4

import pytest
import test_real_home as harness
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_adapters.sandbox.policy import Home
from ai_agent_adapters.sandbox.warm import WarmSandbox
from ai_agent_application.agent_workplace.ports import RunnerLimits
from test_docker import World
from test_real_home import BACKGROUND, turn

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HANDS_IMAGE_ID") and os.getenv("AW_RESIDENT_SOAK_SECONDS")),
    reason="explicit hands image and hour-long soak required",
)


def test_resident_hour_soak_with_gateway_and_cpu(monkeypatch, tmp_path):
    image = os.environ["AW_HANDS_IMAGE_ID"]
    seconds = int(os.environ["AW_RESIDENT_SOAK_SECONDS"])
    assert 3600 <= seconds <= 7200
    monkeypatch.setenv("AW_HOME_IMAGE_ID", image)
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", image)
    # The short helper fixture has a 30 CPU-second lifetime rlimit; it is not a
    # production hour-long resident profile. Keep the normal 120-second bound.
    cap = RunnerLimits(120, 1536, 256, 180, 65536, 512 << 20)
    monkeypatch.setattr(harness, "CAP", cap)
    cpu = []
    original = DockerTaskRunner.run

    async def measured(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        cpu.append(result.cpu_seconds)
        return result

    monkeypatch.setattr(DockerTaskRunner, "run", measured)

    async def scenario():
        home = Home("aw-home-" + uuid4().hex, "Synthetic rules", "Synthetic facts", "Starter", 128)
        template = replace(
            World().runner.templates["python-task"],
            image_id=image,
            executable_prefix=PREFIX,
            ceiling=cap,
            model_role_id="activity",
            max_model_calls=4,
            max_prompt_chars=100000,
            max_model_tokens=256,
        )
        receipts = []
        gateways = []

        async def registered(name):
            pass

        async def released(name, removed):
            receipts.append(removed)

        def egress():
            gateway = Egress(image, EgressLimits(max_bytes=8 * 1024 * 1024), lambda row: None)
            gateways.append(gateway)
            return gateway

        slot = WarmSandbox(
            template,
            workspace=False,
            helper=False,
            idle_seconds=60,
            valid=lambda: None,
            registered=registered,
            released=released,
            home=home,
            resident=True,
            resident_idle_seconds=240,
            egress=egress,
        )
        began = time.monotonic()
        turns = 0
        try:
            await slot.prepare()
            container = slot.name
            while time.monotonic() - began < seconds:
                command = (
                    BACKGROUND.replace("sleep 300", "sleep 7200")
                    if turns == 0
                    else (
                        "kill -0 $(cat /workspace/sleeper.pid) && "
                        'test "$(cat /workspace/bg.txt)" = 403 && echo still-alive'
                    )
                )
                answer = "Synthetic soak turn " + str(turns)
                events, provider = await turn(home, command, answer, resident=slot)
                assert [e.detail for e in events if e.kind == "result"] == [answer]
                assert slot.name == container and not slot.closed and slot.turns == turns + 1
                assert len(gateways) == 1 and all(c is not None and c > 0 for c in cpu)
                assert (
                    await cli("inspect", "--format", "{{.State.Running}}", container)
                ).strip() == b"true"
                turns += 1
                observation = dict(
                    elapsed=round(time.monotonic() - began),
                    turns=turns,
                    cpu_seconds=sum(cpu),
                    one_container=True,
                    one_gateway=True,
                )
                (tmp_path / "soak-progress.json").write_text(json.dumps(observation))
                print(json.dumps(observation), flush=True)
                await asyncio.sleep(min(180, max(0, seconds - (time.monotonic() - began))))
            # Real monotonic idle expiry; the observer closes it just as the host owner does.
            while not slot.idle():
                await asyncio.sleep(0.5)
            await slot.close()
            assert receipts == [True] and slot.closed
            assert not (
                await cli("ps", "-a", "--filter", "name=" + container, "--format", "{{.ID}}")
            ).strip()
            assert not (
                await cli(
                    "network",
                    "ls",
                    "--filter",
                    "name=" + gateways[0].network,
                    "--format",
                    "{{.ID}}",
                )
            ).strip()
            print(
                json.dumps(
                    dict(
                        soak_passed=True,
                        seconds=round(time.monotonic() - began),
                        turns=turns,
                        cpu_seconds=sum(cpu),
                    )
                ),
                flush=True,
            )
        finally:
            await slot.close()
            removed = subprocess.run(["docker", "volume", "rm", home.volume], capture_output=True)
            assert removed.returncode == 0

    asyncio.run(scenario())
