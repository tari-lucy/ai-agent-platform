"""W6: the whole workplace profile of workspace v2 is one operator decision (OC-05 mechanism).

Home, shell and resident sandbox ride on the work-session capability itself; scripts and
internet are their own capabilities with the host's automatic ceiling. One preview/execute
grants all of them for the chosen time; revocation stays per right. Synthetic model, no Docker.
"""

import asyncio

from test_activity import WorkModel, prepared
from test_http import post, scope_params
from test_initial_access import execute_command, preview_command, rights

from ai_agent_core.local_host import work_sessions
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.activity import tools

YEAR = 365 * 86400
PROFILE = {
    "workplace.internet",
    "workplace.knowledge",
    "workplace.opencode-session",
    "workplace.scripts",
    "workplace.skills",
}


def test_one_decision_grants_the_whole_workplace_profile(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=365)
        monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: None)
        await enable_work_sessions(
            host.root, image_id="sha256:" + "a" * 64, areas=["projects", "inbox"],
            output_area="projects", ceiling_days=365, helper_enabled=True, egress_enabled=True,
            home_enabled=True, resident_enabled=True, wall_seconds=600, helper_wall_seconds=300,
        )  # fmt: skip
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            assert status["ready"] and status["max_duration_seconds"] == YEAR
            assert {m["capability_id"] for m in status["members"]} == PROFILE

            planned = await post(env, preview_command(host, "profile", YEAR, "Рабочее место."))
            assert planned.status_code == 200, planned.text
            done = await post(
                env, execute_command(host, "profile-grant", planned.json()["resource_id"])
            )
            assert done.status_code == 200, done.text
            assert set(await rights(rt, scope)) == PROFILE
            usable = {t.manifest.capability_id for t in await tools(rt, scope, session=True)}
            assert (
                PROFILE
                <= {
                    t.manifest.capability_id
                    for t in await tools(rt, scope, session=True)
                    if t.available
                }
                <= usable
            )
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count() == 0

    asyncio.run(scenario())
