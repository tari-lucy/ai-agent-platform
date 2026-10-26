"""W2 home on real Docker, opt-in: files persist across containers, nothing outside the volume.

Needs an image built by scripts/refresh_opencode_harness.py (workspace protocol label).
Only synthetic data; the test volumes are removed at the end.
"""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.policy import Home, RunnerTemplate
from test_docker import CAP, World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_HOME_IMAGE_ID"), reason="explicit workspace-capable image ID required"
)


def runner(world: World, home: Home | None) -> DockerTaskRunner:
    template = RunnerTemplate(
        "python-task", os.environ["AW_HOME_IMAGE_ID"], ("/usr/local/bin/python", "-I"), CAP, 65536
    )
    return DockerTaskRunner(
        (template,), world, world, world, authorize=world.authorize,
        lifecycle=world.lifecycle, home=home,
    )  # fmt: skip


FIRST = """
import os, pathlib, stat
home = pathlib.Path('/workspace')
assert (home / 'README.md').read_text() == 'Synthetic starter'
assert (home / 'inbox').is_dir() and (home / 'share').is_dir()
assert pathlib.Path('/guide/AGENTS.md').read_text() == 'Synthetic rules'
facts = pathlib.Path('/guide/connections.md').read_text()
assert facts.startswith('Synthetic facts') and 'МиБ' in facts
assert pathlib.Path('/guide/workspace-readme.md').read_text().endswith('Synthetic starter')
assert stat.S_IMODE(os.stat('/guide').st_mode) == 0o555
assert stat.S_IMODE(os.stat('/guide/AGENTS.md').st_mode) == 0o444
assert os.environ['PYTHONUSERBASE'] == '/workspace/.local'
assert os.environ['PATH'].startswith('/workspace/.local/bin:')
for outside in ('/outside', '/usr/local/lib/planted', '/guide/extra'):
    try:
        pathlib.Path(outside).write_text('x')
    except OSError:
        pass
    else:
        raise AssertionError(outside)
(home / 'projects/synthetic').mkdir(parents=True)
(home / 'projects/synthetic/note.txt').write_text('kept between turns')
(home / '.local/bin').mkdir(parents=True)
tool = home / '.local/bin/synthetic-tool'
tool.write_text('#!/bin/sh\\necho tool works\\n')
tool.chmod(0o755)
(home / 'README.md').write_text('Synthetic own order')
assert 'пока нет' in pathlib.Path('/guide/skills.md').read_text()
(home / 'skills/build-check').mkdir(parents=True)
(home / 'skills/build-check/SKILL.md').write_text(
    '---\\nname: build-check\\ndescription: Проверять сборку перед ответом\\n---\\n# Body\\n')
(home / 'skills/plain').mkdir()
(home / 'skills/plain/SKILL.md').write_text('# Title\\nFirst plain line\\n')
os.symlink('/etc', home / 'skills/door')
print('first')
"""

SECOND = """
import pathlib, subprocess
home = pathlib.Path('/workspace')
assert (home / 'projects/synthetic/note.txt').read_text() == 'kept between turns'
assert (home / 'README.md').read_text() == 'Synthetic own order'
assert pathlib.Path('/guide/workspace-readme.md').read_text().endswith('Synthetic own order')
assert subprocess.run(['synthetic-tool'], capture_output=True, text=True).stdout == 'tool works\\n'
print('second')
"""

OTHER = """
import pathlib
assert not pathlib.Path('/workspace/projects').exists()
print('other')
"""


def test_home_persists_across_containers_and_is_per_volume(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HOME_IMAGE_ID"])

    async def scenario():
        mine, other = ("aw-home-" + uuid4().hex for _ in range(2))
        home = Home(mine, "Synthetic rules", "Synthetic facts", "Synthetic starter", 64)
        try:
            for code, expected, target in (
                (FIRST, "first\n", home),
                (SECOND, "second\n", home),  # a new runner: as after a restart
                (OTHER, "other\n", Home(other, "r", "c", "m", 64)),
            ):
                world = World()
                result = await runner(world, target).run(world.admission, world.task(code))
                assert result.status == "succeeded", result
                assert result.stdout_tail == expected
                assert [phase for _, phase in world.names] == ["register", "removed"]
            # The container is gone, the volume stays, labelled as a home.
            label = await cli("volume", "inspect", mine, "--format", "{{json .Labels}}")
            assert b"ai-agent-platform.home" in label
        finally:
            for name in (mine, other):
                subprocess.run(["docker", "volume", "rm", name], capture_output=True, check=False)

    asyncio.run(scenario())


def test_home_needs_the_workspace_image():
    async def scenario():
        base = os.getenv("AW_SANDBOX_IMAGE_ID")
        if not base:
            pytest.skip("base image without the workspace label not given")
        world = World()
        template = RunnerTemplate("python-task", base, ("/usr/local/bin/python", "-I"), CAP, 65536)
        refused = DockerTaskRunner(
            (template,), world, world, world, authorize=world.authorize,
            lifecycle=world.lifecycle, home=Home("aw-home-" + uuid4().hex, "r", "c", "m", 64),
        )  # fmt: skip
        result = await refused.run(world.admission, world.task("print(1)"))
        assert result.status == "unknown" and world.names == []

    asyncio.run(scenario())
