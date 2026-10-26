"""W3b "hands" image on real Docker, opt-in: AW_HANDS_IMAGE_ID and live internet.

Under the runner's ordinary isolation (read-only root, uid 65532, cap-drop ALL, internal
network) the image's tools work and Chromium reaches a page only through the gateway.
"""

import asyncio
import json
import os

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.egress import Egress, EgressLimits
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits
from test_docker import World

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HANDS_IMAGE_ID") and os.getenv("AW_EGRESS_LIVE")),
    reason="explicit hands image and AW_EGRESS_LIVE=1 (real internet) required",
)

CAP = RunnerLimits(90, 1024, 256, 150, 65536, 256 * 1024 * 1024)
CODE = """
import json, os, subprocess
def run(*command):
    done = subprocess.run(command, capture_output=True, text=True, timeout=60)
    return [done.returncode, (done.stdout + done.stderr)[:120]]
out = {name: run(*command) for name, command in {
    'git': ('git', '--version'),
    'java': ('java', '-version'),
    'node': ('node', '--version'),
    'ffmpeg': ('ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
               'testsrc=duration=1:size=64x64:rate=5', '/work/clip.mp4'),
}.items()}
out['clip'] = os.path.getsize('/work/clip.mp4') if os.path.exists('/work/clip.mp4') else 0
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path='/usr/bin/chromium',
        args=['--no-sandbox', '--disable-dev-shm-usage'],
        proxy={'server': os.environ['HTTP_PROXY']},
    )
    page = browser.new_page()
    page.goto('https://example.com/', timeout=60000)
    out['title'] = page.title()
    browser.close()
print(json.dumps(out))
"""


def test_tools_and_browser_work_inside_the_ordinary_sandbox(monkeypatch):
    image = os.environ["AW_HANDS_IMAGE_ID"]
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", image)  # the fixture world's own template

    async def scenario():
        rows = []
        world = World()

        def egress():
            return Egress(image, EgressLimits(max_bytes=64 * 1024 * 1024), rows.append)

        template = RunnerTemplate("python-task", image, ("/usr/local/bin/python", "-I"), CAP, 65536)
        runner = DockerTaskRunner(
            (template,), world, world, world, authorize=world.authorize,
            lifecycle=world.lifecycle, egress=egress,
        )  # fmt: skip
        result = await runner.run(world.admission, world.task(CODE, CAP))
        assert result.status == "succeeded", result
        out = json.loads(result.stdout_tail)
        for tool in ("git", "java", "node", "ffmpeg"):
            assert out[tool][0] == 0, out
        assert out["clip"] > 0 and out["title"] == "Example Domain", out
        assert any(row["target"] == "example.com:443" and row["status"] == "ok" for row in rows)

    asyncio.run(scenario())
