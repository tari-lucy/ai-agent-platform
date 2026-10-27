"""Real prewarmed OpenCode survives a slow input proof; no paid provider."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.policy import Home
from test_real_home import turn

pytestmark = pytest.mark.skipif(not os.getenv("AW_HOME_IMAGE_ID"), reason="explicit local image")


def test_slow_input_check_does_not_expire_a_freshly_reverified_warm_turn(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])
    original = DockerTaskRunner._attempt

    async def slow(self, admission):
        found = await original(self, admission)
        await asyncio.sleep(6)
        return found

    monkeypatch.setattr(DockerTaskRunner, "_attempt", slow)

    async def scenario():
        home = Home("aw-home-" + uuid4().hex, "Synthetic rules", "Facts", "Starter")
        try:
            events, _ = await turn(home, "printf checked", "Synthetic checked answer", warm=home)
            assert [e.detail for e in events if e.kind == "result"] == ["Synthetic checked answer"]
        finally:
            subprocess.run(["docker", "volume", "rm", home.volume], capture_output=True)

    asyncio.run(scenario())
