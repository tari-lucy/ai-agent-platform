"""Real Desktop proxy: exhaustion from the owner ledger and a simulated next UTC day."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.egress import EgressLimits
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from test_docker import CAP, World

from ai_agent_core.m3_composition import work_session_egress as owner

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HANDS_IMAGE_ID") and os.getenv("AW_EGRESS_LIVE")),
    reason="explicit Desktop image and live example.com access required",
)
CODE = """
import urllib.request, urllib.error
try:
    with urllib.request.urlopen('http://example.com/', timeout=20) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code, e.read(200).decode())
"""


def test_real_gateway_daily_exhaustion_and_next_day(monkeypatch, tmp_path):
    image = os.environ["AW_HANDS_IMAGE_ID"]
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", image)

    async def scenario():
        settings = owner.EgressSettings(
            "workplace.internet", image, EgressLimits(max_bytes=8 << 20), tmp_path, 5 << 20
        )
        # Seed the trusted owner's ledger rather than downloading 5 MiB as test traffic.
        owner.log_writer(settings, "synthetic", "already-spent")(
            dict(
                method="GET",
                target="http://example.com/",
                status="ok",
                up=0,
                down=5 << 20,
                seconds=0.1,
            )
        )
        world = World()
        template = RunnerTemplate("python-task", image, ("/usr/local/bin/python", "-I"), CAP, 65536)
        runner = DockerTaskRunner(
            (template,),
            world,
            world,
            world,
            authorize=world.authorize,
            lifecycle=world.lifecycle,
            egress=owner.resident_factory(settings, "synthetic"),
        )
        result = await runner.run(world.admission, world.task(CODE, CAP))
        assert result.status == "succeeded"
        assert "403" in result.stdout_tail and "EGRESS_DAILY_LIMIT" in result.stdout_tail

        tomorrow = datetime.now(UTC) + timedelta(days=1)

        class Tomorrow(datetime):
            @classmethod
            def now(cls, tz=None):
                return tomorrow.astimezone(tz)

        monkeypatch.setattr(owner, "datetime", Tomorrow)
        assert owner.used_today(settings, "synthetic") == 0
        result = await runner.run(world.admission, world.task(CODE, CAP))
        assert result.status == "succeeded" and result.stdout_tail.strip() == "200"

    asyncio.run(scenario())
