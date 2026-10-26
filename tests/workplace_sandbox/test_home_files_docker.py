"""W4 operator access to a home volume on real Docker, opt-in; synthetic data only."""

import asyncio
import os
import subprocess
from uuid import uuid4

import pytest
from ai_agent_adapters.sandbox.docker import cli
from ai_agent_adapters.sandbox.home_files import HomeFiles, logical

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_HOME_IMAGE_ID"), reason="explicit workspace-capable image ID required"
)

SEED = """
import os, pathlib
home = pathlib.Path('/workspace')
(home / 'inbox').mkdir()
(home / 'projects/demo').mkdir(parents=True)
(home / 'projects/demo/note.md').write_text('Синтетическая заметка')
(home / 'README.md').write_text('Synthetic readme')
os.symlink('/etc/passwd', home / 'escape')
os.symlink('/etc', home / 'door')
"""


def test_list_read_and_inbox_upload_without_following_links():
    async def scenario():
        image = os.environ["AW_HOME_IMAGE_ID"]
        volume = "aw-home-" + uuid4().hex
        files = HomeFiles(image)
        try:
            assert await files.list(volume, "") == {"status": "ok", "entries": [], "complete": True}
            await cli("volume", "create", volume)
            await cli(
                "run", "--rm", "--network=none", "--user=65532:65532",
                "--mount", f"type=volume,src={volume},dst=/workspace",
                "--entrypoint=/usr/local/bin/python", image, "-I", "-c", SEED,
            )  # fmt: skip
            root = await files.list(volume, "")
            kinds = {row["name"]: row["type"] for row in root["entries"]}
            assert kinds == {
                "inbox": "directory", "projects": "directory", "README.md": "file",
                "escape": "link", "door": "link",
            }  # fmt: skip
            nested = await files.list(volume, "/projects/demo/")
            assert [row["name"] for row in nested["entries"]] == ["note.md"]
            note = await files.read(volume, "projects/demo/note.md")
            import base64

            assert base64.b64decode(note["content"]).decode() == "Синтетическая заметка"
            # Links are shown, never followed: neither as a file nor as a directory.
            assert (await files.read(volume, "escape"))["status"] == "denied"
            assert (await files.list(volume, "door"))["status"] == "denied"
            assert (await files.read(volume, "missing.txt"))["status"] == "missing"
            assert (await files.read(volume, "projects"))["status"] == "not_file"

            assert (await files.put(volume, "Отчёт.txt", b"from operator"))["status"] == "ok"
            assert (await files.put(volume, "Отчёт.txt", b"again"))["status"] == "exists"
            got = await files.read(volume, "inbox/Отчёт.txt")
            assert base64.b64decode(got["content"]) == b"from operator"
            for bad in ("../x", "a/b", "", "."):
                with pytest.raises(ValueError):
                    await files.put(volume, bad, b"x")
        finally:
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)

    asyncio.run(scenario())


def test_logical_paths():
    assert logical("") == logical("/") == ""
    assert logical("/projects//demo/") == "projects/demo"
    for bad in ("..", "a/../b", "./a", "a\x00b", "a\nb", "x" * 256):
        with pytest.raises(ValueError):
            logical(bad)
