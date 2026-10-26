"""Real Desktop resident policy: revoke the idle gateway, then STOP the offline slot."""

import asyncio
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest
import quality_support
from test_activity import WorkModel, prepared
from test_http import post
from test_initial_access import execute_command, preview_command, rights
from test_library_host import decide
from test_session_launcher import stop

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_home import volume

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.getenv("AW_HOME_IMAGE_ID"), reason="Desktop image required"
)


async def wait_for(read, done, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if done(value):
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("Resident transition did not complete")


def test_idle_internet_revocation_closes_gateway_and_stop_closes_offline_slot(
    tmp_path, monkeypatch
):
    async def scenario():
        original = quality_support.initialise

        async def isolated(root, **kwargs):
            return await original(
                root, **{**kwargs, "workspace_id": "resident-desktop-" + uuid4().hex}
            )

        monkeypatch.setattr(quality_support, "initialise", isolated)
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=7)
        await enable_work_sessions(
            host.root,
            image_id=os.environ["AW_HOME_IMAGE_ID"],
            areas=["inbox", "projects"],
            output_area="projects",
            ceiling_days=7,
            home_enabled=True,
            resident_enabled=True,
            egress_enabled=True,
            prewarm_idle_seconds=300,
        )
        target = volume(host.scope)
        try:
            async with host.open():
                rt = host.runtime
                plan = await post(host.env, preview_command(host, "resident-profile", 86400))
                assert plan.status_code == 200
                granted = await post(
                    host.env, execute_command(host, "resident-grant", plan.json()["resource_id"])
                )
                assert granted.status_code == 200
                await host.start()
                controller = rt.session_harness.__self__
                warm = controller.warm
                online = await wait_for(
                    lambda: warm.slot, lambda s: s is not None and s.ready and s.internet
                )
                right = (await rights(rt, host.scope))["workplace.internet"]
                await decide(
                    host,
                    "resident-revoke",
                    dict(
                        action="revoke",
                        capability_id=right.capability_id,
                        binding_id=right.binding_id,
                        permission_id=right.id,
                        permission_version=right.version,
                    ),
                    right.version,
                )
                await wait_for(lambda: online.closed, bool)
                from ai_agent_adapters.sandbox.docker import cli

                assert not (
                    await cli(
                        "network",
                        "ls",
                        "--filter",
                        "name=" + online.gateway.network,
                        "--format",
                        "{{.ID}}",
                    )
                ).strip()
                offline = await wait_for(
                    lambda: warm.slot,
                    lambda s: s is not None and s is not online and s.ready and not s.internet,
                )
                await stop(host, "resident-stop")
                await wait_for(
                    lambda: rt.supervisor.snapshot,
                    lambda r: r.state.value == "STOPPED" and not r.unconfirmed_resource_ids,
                )
                assert offline.closed and model.count() == 0
        finally:
            result = subprocess.run(["docker", "volume", "rm", target], capture_output=True)
            assert result.returncode == 0

    asyncio.run(scenario())
