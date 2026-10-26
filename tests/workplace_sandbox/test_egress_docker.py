"""W3 egress gateway on real Docker, opt-in: needs an image and live internet (example.com).

The agent container reaches the internet only through the proxy; private, link-local,
loopback-of-host and denied destinations are refused; proxy and network go with the turn.
"""

import asyncio
import os

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from test_docker import CAP, World

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_SANDBOX_IMAGE_ID") and os.getenv("AW_EGRESS_LIVE")),
    reason="explicit image and AW_EGRESS_LIVE=1 (real internet) required",
)

CODE = """
import json, os, socket, urllib.error, urllib.request
proxy = os.environ['HTTP_PROXY']
assert os.environ['https_proxy'] == proxy and os.environ['NO_PROXY'].startswith('127.0.0.1')

def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read(200)
    except urllib.error.HTTPError as error:
        return error.code, error.read(200)
    except OSError as error:
        return 'error', str(error).encode()

out = {}
out['http'] = fetch('http://example.com/?q=1')[0]
out['https'] = fetch('https://example.com/')[0]
for name, url in {
    'metadata': 'http://169.254.169.254/latest/meta-data/',
    'private': 'http://10.0.0.1/',
    'bridge': 'http://172.17.0.1/',
    'mail': 'http://example.com:25/',
}.items():
    status, body = fetch(url)
    out[name] = [status, body.decode('latin-1')]
try:
    socket.gethostbyname('example.com')
    out['dns'] = 'resolved'
except OSError:
    out['dns'] = 'none'
s = socket.socket()
s.settimeout(2)
out['direct'] = s.connect_ex(('1.1.1.1', 443))
print(json.dumps(out))
"""


def test_agent_reaches_internet_only_through_the_gateway():
    async def scenario():
        rows = []
        made = []
        world = World()
        image = os.environ["AW_SANDBOX_IMAGE_ID"]
        limits = EgressLimits(max_bytes=4 * 1024 * 1024)

        def egress():
            made.append(Egress(image, limits, rows.append))
            return made[-1]

        cap = CAP.__class__(**{**CAP.__dict__, "wall_seconds": 60, "cpu_seconds": 30})
        template = RunnerTemplate("python-task", image, ("/usr/local/bin/python", "-I"), cap, 65536)
        runner = DockerTaskRunner(
            (template,), world, world, world, authorize=world.authorize,
            lifecycle=world.lifecycle, egress=egress,
        )  # fmt: skip
        result = await runner.run(world.admission, world.task(CODE, cap))
        assert result.status == "succeeded", result
        import json

        out = json.loads(result.stdout_tail)
        assert out["http"] == 200 and out["https"] == 200
        for name in ("metadata", "private", "bridge"):
            assert out[name][0] == 403 and "EGRESS_DESTINATION_DENIED" in out[name][1], out
        assert out["mail"][0] == 403 and "EGRESS_PORT_DENIED" in out["mail"][1]
        assert out["dns"] == "none" and out["direct"] != 0
        # The log: full URL for plain HTTP, host:port for the HTTPS tunnel.
        targets = {(row["method"], row["target"], row["status"]) for row in rows}
        assert ("GET", "http://example.com/?q=1", "ok") in targets
        assert ("CONNECT", "example.com:443", "ok") in targets
        assert sum(row["status"] == "denied" for row in rows) == 4
        # Agent container, proxy and network all removed with the turn.
        phases = [phase for _, phase in world.names]
        assert phases.count("register") == 2 and phases.count("removed") == 2
        gone = await cli("ps", "--all", "--filter", "name=" + made[0].name, "--format", "{{.ID}}")
        net = await cli("network", "ls", "--filter", "name=" + made[0].network, "--format", "{{.ID}}")
        assert not gone.strip() and not net.strip()

    asyncio.run(scenario())
