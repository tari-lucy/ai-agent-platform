"""Desktop hands inside real OpenCode, not just a bare Python runner; no paid model."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
import test_real_home as harness
from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_adapters.sandbox.policy import Home
from ai_agent_application.agent_workplace.ports import RunnerLimits
from test_hands_docker import CODE

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HANDS_IMAGE_ID") and os.getenv("AW_EGRESS_LIVE")),
    reason="explicit hands image and live example.com access required",
)


def test_chromium_through_real_opencode(monkeypatch):
    image = os.environ["AW_HANDS_IMAGE_ID"]
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", image)
    monkeypatch.setenv("AW_HOME_IMAGE_ID", image)
    monkeypatch.setattr(harness, "CAP", RunnerLimits(100, 2048, 256, 120, 65536, 512 << 20))

    async def scenario():
        rows = []
        home = Home("aw-home-" + uuid4().hex, "Synthetic rule", "Synthetic facts", "Starter")
        # The identical browser script that passed the isolated Python test, inside bash.
        command = "XDG_CONFIG_HOME=/work/browser-config python3 - <<'PY'\n" + CODE + "\nPY"
        try:
            events, provider = await harness.turn(
                home,
                command,
                "Browser checked",
                egress=lambda: Egress(image, EgressLimits(max_bytes=8 << 20), rows.append),
            )
            assert [e.detail for e in events if e.kind == "result"] == ["Browser checked"]
            output = harness.tool_output(provider)
            assert "Example Domain" in output, output[-3000:]
            assert any(r["target"] == "example.com:443" for r in rows)
        finally:
            subprocess.run(["docker", "volume", "rm", home.volume], capture_output=True)

    asyncio.run(scenario())
