"""Opt-in P1: real Docker/OpenCode + isolated production app + local scripted model.

No personal root, .env or external provider. Only resources created here are removed.
"""

import asyncio
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE

from ai_agent_core.local_host.activity import configure_activity
from ai_agent_core.local_host.harness import configure
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import LiveConfig, load_config, paths
from ai_agent_core.local_host.memory import enable_memory
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.serve import build
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Docker Desktop acceptance"),
    pytest.mark.skipif(
        not os.getenv("AW_P1_IMAGE_ID"), reason="explicit locally verified Docker image required"
    ),
]

MODEL = (Path(__file__).parent / "fixtures/p1_model_server.py").read_text(encoding="utf-8")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def docker(*args):
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, error = await asyncio.wait_for(process.communicate(), 60)
    if process.returncode:
        raise AssertionError(error.decode(errors="replace"))
    return out.decode().strip()


async def until(read, predicate, seconds=45):
    for _ in range(seconds * 2):
        value = await read()
        if predicate(value):
            return value
        await asyncio.sleep(0.5)
    raise AssertionError("P1 condition timed out")


def test_real_doc33_files_memory_wake_restart_and_stop(tmp_path):
    async def scenario():
        image = os.environ["AW_P1_IMAGE_ID"]
        suffix = uuid4().hex[:10]
        fixture = "p1-model-" + suffix
        originals = set(
            (
                await docker(
                    "ps",
                    "-a",
                    "--filter",
                    "label=ai-agent-platform.harness=1",
                    "--format",
                    "{{.ID}}",
                )
            ).splitlines()
        )
        await docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            fixture,
            "--env=P1_FIXTURE_ONLY=1",
            "--network=bridge",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=128m",
            "--pids-limit=64",
            "--entrypoint=python",
            image,
            "-I",
            "-u",
            "-c",
            MODEL,
        )
        model_ip = await docker(
            "inspect",
            "--format",
            '{{(index .NetworkSettings.Networks "bridge").IPAddress}}',
            fixture,
        )
        server = task = closer = native = client = None
        volume = None
        try:
            root = tmp_path / "host"
            panel_port, native_port = port(), port()
            await initialise(
                root,
                profile_markdown=REFERENCE_PROFILE,
                workspace_id="p1-windows",
                persona_id="p1-" + suffix,
                operator_controls=True,
                port=panel_port,
                live=LiveConfig(
                    model_id="synthetic-p1", attempt_limit=300, attempt_period="utc_day"
                ),
            )
            await enable_memory(root)
            await enable_recovery(
                root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
            )
            await enable_tickets(root)
            await configure_activity(root, enabled=True)
            await enable_workplace_files(root)
            await enable_work_sessions(
                root,
                image_id=image,
                areas=["projects", "inbox"],
                output_area="projects",
                home_enabled=True,
                wake_enabled=True,
            )
            files = tmp_path / "agent-files"
            settings = configure(
                root,
                enabled=True,
                port=native_port,
                files_dir=str(files),
                input_rate=16,
                output_rate=80,
            )
            assert settings.enabled
            config = load_config(paths(root))
            home = files / config.persona_id
            for name in ("inbox", "projects"):
                (home / name).mkdir(parents=True, exist_ok=True)
            (home / "inbox/p1-marker.txt").write_text("P1-MARKER", encoding="utf-8")
            env_file = tmp_path / "synthetic.env"
            env_file.write_text(
                "AI_PLATFORM_LLM_ENDPOINT=https://provider.invalid/v1\nAI_PLATFORM_LLM_API_KEY=synthetic-p1-model-key\n"
            )

            async def opened():
                nonlocal server, task, closer, client, native, volume
                app, closer = build(
                    paths(root), load_config(paths(root)), provider="live", env_file=env_file
                )
                harness = app.state.runtime.harness
                assert harness is not None
                # Only the synthetic upstream is substituted; owners/lifecycle/MCP are real.
                harness.settings = replace(harness.settings, upstream=f"http://{model_ip}:41412/v1")
                server = uvicorn.Server(
                    uvicorn.Config(
                        app, host="127.0.0.1", port=panel_port, log_level="error", access_log=False
                    )
                )
                task = asyncio.create_task(server.serve())

                async def started():
                    return server.started

                await until(started, bool)
                client = httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{panel_port}", trust_env=False, timeout=20
                )
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"password": paths(root).password.read_text().strip()},
                    headers={"Origin": f"http://127.0.0.1:{panel_port}"},
                )
                login.raise_for_status()
                client.headers.update(
                    {
                        "Origin": f"http://127.0.0.1:{panel_port}",
                        "X-CSRF-Token": login.json()["csrf_token"],
                    }
                )
                native = httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{native_port}",
                    auth=("opencode", harness.settings.password),
                    trust_env=False,
                    timeout=60,
                )
                volume = harness.home()
                return app.state.runtime

            async def closed():
                nonlocal closer, client, native, server, task
                if native:
                    await native.aclose()
                if client:
                    await client.aclose()
                if server:
                    server.should_exit = True
                    await asyncio.wait_for(task, 25)
                if closer:
                    closer()
                server = task = closer = client = native = None

            rt = await opened()

            async def start():
                run = rt.supervisor.snapshot
                assert run.state.value == "STOPPED"
                r = await client.post(
                    "/api/v1/commands",
                    json={
                        "type": "agent.start",
                        "request_id": uuid4().hex,
                        "target": run.agent_id,
                        "scope": run.scope.model_dump(mode="json"),
                        "expected_version": run.version,
                        "arguments": {"autonomy": "autonomous"},
                    },
                )
                assert r.status_code == 200, r.text

                async def ready():
                    return rt.harness.current()

                await until(ready, bool)

            async def stop():
                run = rt.supervisor.snapshot
                r = await client.post(
                    f"/api/v1/agents/{run.agent_id}/stop",
                    json={
                        "type": "agent.stop",
                        "request_id": uuid4().hex,
                        "target": run.agent_id,
                        "scope": run.scope.model_dump(mode="json"),
                    },
                )
                assert r.status_code == 200, r.text

                async def state():
                    return rt.supervisor.snapshot.state.value

                await until(state, lambda x: x == "STOPPED")
                assert not rt.supervisor.snapshot.unconfirmed_resource_ids

            await start()
            session = await native.post("/session", json={"title": "P1 synthetic acceptance"})
            session.raise_for_status()
            sid = session.json()["id"]

            async def say(text):
                r = await native.post(
                    f"/session/{sid}/message", json={"parts": [{"type": "text", "text": text}]}
                )
                assert r.status_code == 200, r.text
                assert not r.json().get("info", {}).get("error"), r.text
                return r.json()

            reply = await say("P1-FILE: synthetic filesystem check")
            assert (home / "projects/p1-result.txt").read_text() == "P1-RESULT"
            assert any("P1-MARKER" in p.get("text", "") for p in reply["parts"])
            query = {
                k: v
                for k, v in rt.supervisor.snapshot.scope.model_dump(mode="json").items()
                if k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
            }

            async def sources():
                return (await client.get("/api/v1/sources", params=query)).json()["items"]

            stored = await until(
                sources, lambda xs: any("P1-FILE" in x.get("content", "") for x in xs)
            )
            ids = {s["id"] for s in stored}
            await asyncio.sleep(16)
            assert {s["id"] for s in await sources()} == ids
            await say("P1-WAKE: set one wake_me for one minute")

            async def alarms():
                rows = (await native.get(f"/session/{sid}/message")).json()
                return [
                    m["info"]["id"]
                    for m in rows
                    if m["info"]["role"] == "user"
                    and any("P1-ALARM" in p.get("text", "") for p in m["parts"])
                ]

            first = await until(alarms, bool, seconds=100)
            assert len(first) == 1
            await stop()
            await closed()
            rt = await opened()
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await start()
            await asyncio.sleep(16)
            assert await alarms() == first
            pending = asyncio.create_task(say("P1-STOP: synthetic foreground stop check"))

            async def running():
                rows = (await native.get(f"/session/{sid}/message")).json()
                return any(
                    p.get("tool") == "bash" and p.get("state", {}).get("status") == "running"
                    for m in rows
                    for p in m["parts"]
                )

            await until(running, bool)
            await stop()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            assert not (home / "projects/p1-late.txt").exists()
            await closed()
        finally:
            if server:
                server.should_exit = True
                await asyncio.wait_for(task, 25)
            if closer:
                closer()
            if native:
                await native.aclose()
            if client:
                await client.aclose()
            await docker("rm", "-f", fixture)
            if volume:
                await docker("volume", "rm", volume)
            after = set(
                (
                    await docker(
                        "ps",
                        "-a",
                        "--filter",
                        "label=ai-agent-platform.harness=1",
                        "--format",
                        "{{.ID}}",
                    )
                ).splitlines()
            )
            assert after == originals, "a pre-existing harness resource changed"

    asyncio.run(scenario())
