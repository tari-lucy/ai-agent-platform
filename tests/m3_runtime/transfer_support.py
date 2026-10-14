"""Synthetic operator-accessible work/test settings, no real env or provider IO."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.persona import CharacterState, GmRuntimeState, Profile
from composition_support import setup
from fastapi import FastAPI

from ai_agent_core.access import install_access
from ai_agent_core.m3_composition.state import PRINCIPALS, put


@asynccontextmanager
async def prepared(tmp_path, *, origin=ORIGIN, realtime=False, source_api=True):
    env = await setup(tmp_path, start=False)
    rt, store = env.runtime, env.world.store
    if realtime:
        env.world.clock.now = datetime.now(UTC)
    target = env.world.scope
    source = target.model_copy(update={"memory_mode": "test", "memory_space_id": "transfer-test"})
    operator = Principal("operator", "operator")
    readers = rt.readers + (RecordAccess("operator", api_transfer=True),)
    grants = env.grants + tuple(
        Grant(operator, o, read=True, api_transfer=True)
        for o in (Owner.PERSONA, Owner.GM, Owner.WORKSPACE)
    )
    await store.provision(target, grants)
    await store.provision(
        source,
        tuple(
            Grant(
                g.principal,
                g.owner,
                read=g.read,
                write=g.write,
                api_transfer=False
                if g.principal.id == "operator" and not source_api
                else g.api_transfer,
            )
            for g in grants
        ),
    )
    rt.readers = readers
    rt.scopes += (source,)
    for model, owner in (
        (Profile, Owner.PERSONA),
        (CharacterState, Owner.PERSONA),
        (GmRuntimeState, Owner.GM),
    ):
        rows = await store.repository(model, PRINCIPALS[owner], target).list()
        for old in rows:
            changes = {"version": old.version + 1}
            if isinstance(old, CharacterState):
                changes.update(state_version=old.version + 1, profile_version=2)
            await put(
                store,
                rt.registry,
                owner,
                target,
                "transfer-acl-" + old.id,
                old.model_copy(update=changes),
                old.version,
                readers,
            )
            if isinstance(old, Profile):
                from ai_agent_application.persona import PersonaService

                sample = old.model_copy(
                    update={"scope": source, "canon": "Вымышленный тестовый канон."}
                )
                parsed = PersonaService(store, PRINCIPALS[owner], rt.registry)._parsed(sample)
                sample = sample.model_copy(update={"accepted_revision_id": parsed.revision_id})
                await put(store, rt.registry, owner, source, "test-profile", sample, None, readers)
            if isinstance(old, GmRuntimeState):
                sample = old.model_copy(
                    update={"scope": source, "intensity": 0.9, "rules_version": 2}
                )
                await put(store, rt.registry, owner, source, "test-gm", sample, None, readers)
    app = FastAPI()
    install_access(
        app,
        rt.supervisor,
        BootstrapSettings(operator_origin=origin, operator_password_hash=configured_hash()),
        rt.access_ports(),
    )
    env.app, env.source, env.target = app, source, target
    env.readers, env.fixture_grants = readers, grants

    def command(keys=("gm_intensity",), operation="transfer-1", request="preview-1"):
        return {
            "type": "laboratory.transfer_settings",
            "request_id": request,
            "target": "laboratory",
            "scope": target.model_dump(mode="json"),
            "expected_version": 2,
            "arguments": {
                "schema_id": "laboratory.transfer-settings-args",
                "data": {
                    "action": "preview",
                    "source_scope": source.model_dump(mode="json"),
                    "keys": list(keys),
                    "operation_id": operation,
                },
            },
        }

    env.command = command
    env.query = dict(
        zip(
            ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
            target.partition,
            strict=True,
        )
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": origin}
            )
            assert login.status_code == 200
            env.client, env.headers = (
                client,
                {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
            )
            yield env
    finally:
        await rt.close()


def apply_command(env, plan):
    command = env.command(
        plan["keys"], operation=plan["operation_id"], request="apply-" + plan["operation_id"]
    )
    command["expected_version"] = plan["target_version"]
    command["arguments"]["data"].update(
        action="apply",
        preview_digest=plan["preview_digest"],
        plan_id=plan["id"],
        plan_version=plan["version"],
    )
    return command
