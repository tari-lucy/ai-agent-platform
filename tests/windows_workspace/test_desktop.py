"""Windows/Docker Desktop acceptance gaps; synthetic data and explicitly pinned images."""

import asyncio
import json
import os
import sys
import time
from dataclasses import replace

import pytest
import test_home_files_docker as home_probe
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_adapters.sandbox.home_files import HomeFiles
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from test_docker import CAP, World

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.getenv("AW_HOME_IMAGE_ID"),
    reason="Windows and pinned home image required",
)


@pytest.fixture(autouse=True)
def pinned_image(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])


def test_desktop_hostnames_are_denied_before_connecting():
    async def scenario():
        world = World()
        image = os.environ["AW_HOME_IMAGE_ID"]
        rows = []
        cap = replace(CAP, wall_seconds=30, cpu_seconds=10)
        template = RunnerTemplate("python-task", image, ("/usr/local/bin/python", "-I"), cap, 65536)
        gateway = Egress(image, EgressLimits(), rows.append)
        runner = DockerTaskRunner(
            (template,),
            world,
            world,
            world,
            authorize=world.authorize,
            lifecycle=world.lifecycle,
            egress=lambda: gateway,
        )
        code = """import json,urllib.request,urllib.error
out={}
for host in ('host.docker.internal','gateway.docker.internal','169.254.169.254','[::1]'):
    try:
        urllib.request.urlopen('http://'+host+'/',timeout=5)
    except urllib.error.HTTPError as e:
        out[host]=[e.code,e.read(200).decode()]
print(json.dumps(out))
"""
        result = await runner.run(world.admission, world.task(code, cap))
        assert result.status == "succeeded", result
        outcomes = json.loads(result.stdout_tail)
        assert len(outcomes) == 4
        assert all(
            v[0] == 403 and "EGRESS_DESTINATION_DENIED" in v[1] for v in outcomes.values()
        ), outcomes
        assert len(rows) == 4 and all(r["status"] == "denied" for r in rows)
        assert [phase for _, phase in world.names].count("removed") == 2

    asyncio.run(scenario())


def test_home_file_roundtrips_on_desktop(monkeypatch):
    # Reuse the owner's complete real-file scenario, adding only Desktop timings.
    original = HomeFiles._run
    timings = []

    async def measured(self, volume, request, data=b""):
        started = time.perf_counter()
        answer = await original(self, volume, request, data)
        timings.append((request["op"], round(time.perf_counter() - started, 3)))
        return answer

    monkeypatch.setattr(HomeFiles, "_run", measured)
    home_probe.test_list_read_and_inbox_upload_without_following_links()
    assert timings
    print("Desktop file roundtrips:", json.dumps(timings))


def test_home_quota_is_a_warning_not_a_filesystem_limit():
    import subprocess
    from uuid import uuid4

    from ai_agent_adapters.sandbox.policy import Home

    async def scenario():
        world = World()
        image = os.environ["AW_HOME_IMAGE_ID"]
        cap = replace(CAP, wall_seconds=15, cpu_seconds=5)
        template = RunnerTemplate("python-task", image, ("/usr/local/bin/python", "-I"), cap, 65536)
        home = Home("aw-home-" + uuid4().hex, "Synthetic rule", "Synthetic facts", "Starter", 64)
        runner = DockerTaskRunner(
            (template,),
            world,
            world,
            world,
            authorize=world.authorize,
            lifecycle=world.lifecycle,
            home=home,
        )
        try:
            first = await runner.run(
                world.admission,
                world.task(
                    "from pathlib import Path; p=Path('/workspace'); assert p.stat().st_uid==65532;"
                    "f=(p/'large.bin').open('wb'); "
                    "[f.write(b'x'*1024*1024) for _ in range(65)]; f.close(); print('written')",
                    cap,
                ),
            )
            assert first.status == "succeeded", first
            second = await runner.run(
                world.admission,
                world.task(
                    "from pathlib import Path; t=Path('/guide/connections.md').read_text(); "
                    "assert 'Порог превышен' in t; print('warning present')",
                    cap,
                ),
            )
            assert second.status == "succeeded", second
        finally:
            result = subprocess.run(["docker", "volume", "rm", home.volume], capture_output=True)
            assert result.returncode == 0

    asyncio.run(scenario())
