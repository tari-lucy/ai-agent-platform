"""Existing scripts configuration is frozen when home is enabled, and thaws without it."""

import asyncio

from test_activity import WorkModel, prepared
from test_http import scope_params

from ai_agent_core.local_host import work_sessions
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.activity import tools


def test_existing_scripts_are_frozen_and_leave_the_access_card(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=365)
        monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: None)
        args = dict(
            image_id="sha256:" + "a" * 64,
            areas=["projects", "inbox"],
            output_area="projects",
            ceiling_days=365,
            helper_enabled=True,
        )
        await enable_work_sessions(host.root, **args)
        await enable_work_sessions(host.root, **args, home_enabled=True, egress_enabled=True)
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            # Frozen (W7b): disabled records, no ceiling, so not in the access card either.
            assert "workplace.scripts" not in {m["capability_id"] for m in status["members"]}
            assert rt.session_helper_capability is None
            assert not any(
                t.available and t.manifest.capability_id == "workplace.scripts"
                for t in await tools(rt, scope, session=True)
            )
        # Without the home again, the same records are ready again (no conflict).
        await enable_work_sessions(host.root, **args, home_enabled=False)
        async with host.open():
            rt, scope = host.runtime, host.scope
            assert rt.session_helper_capability == "workplace.scripts"
            assert any(
                t.manifest.capability_id == "workplace.scripts"
                for t in await tools(rt, scope, session=True)
            )

    asyncio.run(scenario())
