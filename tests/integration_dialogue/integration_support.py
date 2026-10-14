"""Explicit synthetic host setup shared by ASGI and live browser acceptance."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import Permission
from ai_agent_contracts.persona import CharacterState, GmRuntimeState, Profile
from composition_support import setup

from ai_agent_core.m3_composition.app import create_m3_app
from ai_agent_core.m3_composition.state import PRINCIPALS, put


async def prepare_app(tmp_path, scripts=(), *, origin=ORIGIN):
    env = await setup(tmp_path, scripts, start=False)
    await env.runtime.close()
    world = env.world
    scope = world.scope
    operator = Principal("operator", "operator")
    owners = (
        Owner.PERSONA,
        Owner.MEMORY,
        Owner.GATEWAY,
        Owner.CHANNEL,
        Owner.JOBS,
        Owner.LLM,
        Owner.GM,
    )
    await world.store.provision(
        scope,
        env.grants
        + (Grant(PRINCIPALS[Owner.GATEWAY], Owner.MEMORY, read=True),)
        + tuple(Grant(operator, o, read=True, api_transfer=True) for o in owners),
    )
    readers = (RecordAccess("operator", api_transfer=True),) + tuple(
        RecordAccess(PRINCIPALS[o].id, api_transfer=o in (Owner.GM, Owner.PERSONA, Owner.MEMORY))
        for o in (Owner.GM, Owner.PERSONA, Owner.MEMORY, Owner.JOBS, Owner.BEHAVIOR, Owner.GATEWAY)
    )
    # Explicit fixture ACLs; runtime never provisions or upgrades access.
    profile = await world.store.repository(Profile, PRINCIPALS[Owner.PERSONA], scope).get(
        scope.persona_id
    )
    for model, owner in (
        (Profile, Owner.PERSONA),
        (CharacterState, Owner.PERSONA),
        (GmRuntimeState, Owner.GM),
        (Permission, Owner.WORKSPACE),
    ):
        rows = await world.store.repository(model, PRINCIPALS[owner], scope).list(limit=100)
        for old in rows:
            changes = dict(version=old.version + 1, updated_at=datetime.now(UTC))
            if isinstance(old, CharacterState):
                changes["profile_version"] = profile.version + 1
                changes["state_version"] = old.version + 1
            if isinstance(old, Permission):
                changes["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
            await put(
                world.store,
                world.registry,
                owner,
                scope,
                "fixture-" + old.id,
                old.model_copy(update=changes),
                old.version,
                readers,
            )
    world.clock.now = datetime.now(UTC)
    index = SQLiteRecallIndex(tmp_path / "dialogue-index.sqlite")
    app = create_m3_app(
        BootstrapSettings(
            workspace_id=scope.workspace_id,
            persona_id=scope.persona_id,
            operator_origin=origin,
            operator_password_hash=configured_hash(),
        ),
        world.store,
        world.registry,
        providers=lambda _: world.provider,
        scopes=(scope,),
        reader_access=readers,
        dialogue_index=index,
    )
    env.app, env.index = app, index
    return env


@asynccontextmanager
async def prepared(tmp_path, scripts=()):
    env = await prepare_app(tmp_path, scripts)
    app = env.app
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as client:
                login = await client.post(
                    "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
                )
                assert login.status_code == 200
                env.headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
                env.client, env.runtime = client, app.state.runtime
                yield env
    finally:
        env.index.close()
        await env.world.store.close()
