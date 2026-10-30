"""Production host wiring: GM opt-in survives restart, never starts the agent itself."""

import asyncio

from ai_agent_contracts.states import RunState
from private_gm_support import LINE, GmModel, due, gm_host

from ai_agent_core.local_host.gm import configure_gm


def test_explicit_gm_auto_start_is_wired_through_real_host(tmp_path, monkeypatch):
    async def scenario():
        model = GmModel()
        host = await gm_host(
            tmp_path, model, monkeypatch, enabled=True, auto_start=True, line_direction=LINE
        )
        for _ in range(2):
            async with host.open():
                assert host.runtime.supervisor.snapshot.state is RunState.STOPPED
                assert host.runtime.supervisor.snapshot.gm_paused
                await host.start()
                assert not host.runtime.supervisor.snapshot.gm_paused
                assert await due(host) == "NO_EXPERIENCE"
                assert model.count() == 0  # no fake experience or call just to prove startup
        await configure_gm(host.root, auto_start=False)
        async with host.open():
            await host.start()
            assert host.runtime.supervisor.snapshot.gm_paused
            assert model.count() == 0

    asyncio.run(scenario())
