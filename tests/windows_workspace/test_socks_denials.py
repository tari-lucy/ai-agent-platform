"""Real Docker Desktop SOCKS refusals, including its special host DNS name."""

import asyncio
import json
import os

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from test_docker import CAP, World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_HANDS_IMAGE_ID"), reason="explicit Desktop hands image required"
)
CODE = r"""
import json, os, socket
gateway, port = os.environ['AI_WORKPLACE_SOCKS'].removeprefix('socks5h://').rsplit(':', 1)
out = {}
for host, dest in [('169.254.169.254', 80), ('10.0.0.1', 80),
                   ('host.docker.internal', 80), ('example.com', 25)]:
    with socket.create_connection((gateway, int(port)), timeout=10) as s:
        s.sendall(b'\x05\x01\x00')
        assert s.recv(2) == b'\x05\x00'
        raw = host.encode()
        s.sendall(b'\x05\x01\x00\x03' + bytes([len(raw)]) + raw + dest.to_bytes(2))
        reply = s.recv(10)
        out[host] = reply[1]
print(json.dumps(out))
"""


def test_socks_denies_windows_host_metadata_lan_and_mail(monkeypatch):
    image = os.environ["AW_HANDS_IMAGE_ID"]
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", image)

    async def scenario():
        rows = []
        world = World()
        cap = CAP.__class__(**{**CAP.__dict__, "wall_seconds": 60, "cpu_seconds": 30})
        template = RunnerTemplate("python-task", image, ("/usr/local/bin/python", "-I"), cap, 65536)
        runner = DockerTaskRunner(
            (template,),
            world,
            world,
            world,
            authorize=world.authorize,
            lifecycle=world.lifecycle,
            egress=lambda: Egress(image, EgressLimits(), rows.append),
        )
        result = await runner.run(world.admission, world.task(CODE, cap))
        assert result.status == "succeeded", result
        out = json.loads(result.stdout_tail)
        assert len(out) == 4 and all(code != 0 for code in out.values()), out
        assert len(rows) == 4 and all(
            r["method"] == "SOCKS" and r["status"] == "denied" for r in rows
        )
        assert any(r["reason"] == "EGRESS_PORT_DENIED" for r in rows)

    asyncio.run(scenario())
