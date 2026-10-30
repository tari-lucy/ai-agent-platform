"""100 MiB home upload and catalogue-to-runner stdin; no model."""

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.home_files import HomeFiles
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits
from ai_agent_contracts.common import ArtifactRef
from test_docker import World

from ai_agent_core.local_host.cli import parser
from ai_agent_core.local_host.layout import WorkSessionsConfig

SIZE = 100 << 20


def test_input_ceiling_is_configurable_without_new_permission():
    config = WorkSessionsConfig(
        image_id="sha256:" + "a" * 64,
        areas=("inbox",),
        output_area="inbox",
        max_input_bytes=SIZE,
        disk_mb=512,
    )
    args = parser().parse_args(
        [
            "enable-work-sessions",
            "--root",
            "C:/synthetic",
            "--image-id",
            config.image_id,
            "--area",
            "inbox",
            "--output-area",
            "inbox",
            "--max-input-bytes",
            str(SIZE),
        ]
    )
    assert args.max_input_bytes == SIZE


@pytest.mark.skipif(not os.getenv("AW_LARGE_FILE_IMAGE_ID"), reason="explicit local Docker image")
def test_real_hundred_mib_home_and_runner_input(monkeypatch):
    image = os.environ["AW_LARGE_FILE_IMAGE_ID"]
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", image)

    async def scenario():
        data = b"x" * SIZE
        digest = hashlib.sha256(data).hexdigest()
        volume = "aw-home-" + uuid4().hex
        await DockerTaskRunner.ensure_home(volume)
        try:
            await cli(
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--user=65532:65532",
                "--mount",
                f"type=volume,src={volume},dst=/workspace",
                "--entrypoint=/usr/local/bin/python",
                image,
                "-I",
                "-c",
                "from pathlib import Path; Path('/workspace/inbox').mkdir(exist_ok=True)",
            )
            home = HomeFiles(image)
            assert (await home.put(volume, "test.bin", data))["status"] == "ok"
            assert (await home.put(volume, "test.bin", b"do not overwrite"))["status"] == "exists"
            code = (
                "import hashlib,json; p='/workspace/inbox/test.bin'; h=hashlib.sha256(); "
                "f=open(p,'rb'); n=0\n"
                "while b:=f.read(1048576): h.update(b); n+=len(b)\n"
                "print(json.dumps({'size':n,'digest':h.hexdigest()}))"
            )
            verified = json.loads(
                await cli(
                    "run",
                    "--rm",
                    "--network=none",
                    "--read-only",
                    "--user=65532:65532",
                    "--mount",
                    f"type=volume,src={volume},dst=/workspace,readonly",
                    "--entrypoint=/usr/local/bin/python",
                    image,
                    "-I",
                    "-c",
                    code,
                )
            )
            assert verified == {"size": SIZE, "digest": digest}

            w = World()
            ref = ArtifactRef(
                artifact_id="synthetic-large",
                version=1,
                scope=w.admission.scope,
                checksum=digest,
                size_bytes=SIZE,
                media_type="application/octet-stream",
            )
            w.record = w.record.model_copy(update={"input_files": [ref]})

            class Files:
                async def read(self, admission, identifier, version):
                    assert admission == w.admission and (identifier, version) == (
                        ref.artifact_id,
                        1,
                    )
                    return SimpleNamespace(reference=ref), data

            cap = RunnerLimits(90, 2048, 128, 180, 65536, 512 << 20)
            small = RunnerTemplate(
                "python-task", image, ("/usr/local/bin/python", "-I"), cap, 262144
            )
            command = "from pathlib import Path; print(Path('inputs/input-0000').stat().st_size)"
            task = replace(w.task(command, cap), inputs=(ref,))
            denied = DockerTaskRunner(
                (small,),
                Files(),
                w,
                w,
                authorize=w.authorize,
                lifecycle=w.lifecycle,
            )
            result = await denied.run(w.admission, task)
            assert result.status != "succeeded" and not w.names
            allowed = DockerTaskRunner(
                (replace(small, max_input_bytes=SIZE),),
                Files(),
                w,
                w,
                authorize=w.authorize,
                lifecycle=w.lifecycle,
            )
            result = await allowed.run(w.admission, task)
            assert result.status == "succeeded" and result.stdout_tail.strip() == str(SIZE)
        finally:
            removed = subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
            assert removed.returncode == 0

    asyncio.run(scenario())
