"""An app built with its own sign-in lifetime starts and issues that cookie lifetime."""

import httpx
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from composition_support import run, setup

from ai_agent_core.access.sessions import AccessLimits
from ai_agent_core.m3_composition.app import create_m3_app


def test_local_sign_in_lifetime_reaches_the_cookie(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        await env.runtime.close()
        scope = env.world.scope
        settings = BootstrapSettings(
            workspace_id=scope.workspace_id,
            persona_id=scope.persona_id,
            operator_origin=ORIGIN,
            operator_password_hash=configured_hash(),
        )
        app = create_m3_app(
            settings,
            env.world.store,
            env.world.registry,
            providers=lambda _: env.world.provider,
            scopes=(scope,),
            access_limits=AccessLimits(session_seconds=12 * 3600, idle_seconds=3600),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as client:
                login = await client.post(
                    "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
                )
                assert login.status_code == 200
                assert "Max-Age=43200" in login.headers["set-cookie"]

    run(scenario())
