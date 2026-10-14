"""Synthetic active parent and isolated lab with a controllable scenario boundary."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory import LabRunner, lab_grants
from ai_agent_application.laboratory.catalogue import catalogue
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import Permission
from composition_support import setup
from fastapi import FastAPI

from ai_agent_core.access import install_access
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import PRINCIPALS, put


@asynccontextmanager
async def prepared(tmp_path, monkeypatch, *, origin=ORIGIN, realtime=False):
    env = await setup(tmp_path, start=False)
    rt, store = env.runtime, env.world.store
    if realtime:
        permissions = await store.repository(
            Permission, PRINCIPALS[Owner.WORKSPACE], env.world.scope
        ).list(limit=200)
        env.world.clock.now = datetime.now(UTC)
        for old in permissions:
            await put(
                store,
                rt.registry,
                Owner.WORKSPACE,
                env.world.scope,
                "ttl-" + old.id,
                old.model_copy(
                    update={
                        "version": old.version + 1,
                        "expires_at": env.world.clock() + timedelta(hours=1),
                    }
                ),
                old.version,
            )
    lab_scope = env.world.scope.model_copy(
        update={"memory_mode": "test", "memory_space_id": "control-test"}
    )
    grants = env.grants + (
        Grant(Principal("operator", "operator"), Owner.JOBS, read=True, api_transfer=True),
    )
    await store.provision(env.world.scope, grants)
    await store.provision(lab_scope, grants + lab_grants())
    rt.scopes += (lab_scope,)
    rt.jobs.store.readers += (RecordAccess("operator", api_transfer=True),)
    env.authority = ControlAuthority("operator", frozenset(s.partition for s in rt.scopes), 3600)
    env.entered, env.release = asyncio.Event(), asyncio.Event()
    first = catalogue()[0]

    async def controlled(lab):
        env.lab = lab
        env.entered.set()
        await env.release.wait()
        lab.check("SYNTHETIC_SCENARIO_FINISHED", True)

    scenario = replace(first, run=controlled)
    import ai_agent_core.m3_composition.service as service

    monkeypatch.setattr(service, "catalogue", lambda: (scenario,))

    async def allocate(label):
        return lab_scope

    rt.reports = Reports(
        tmp_path / "reports",
        store,
        rt.registry,
        clock=env.world.clock,
        readers=(RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),),
    )
    rt.lab = LabRunner(
        store,
        allocate,
        lambda: SQLiteRecallIndex(tmp_path / "lab-index.sqlite"),
        artifact_writer=rt.reports.save,
    )
    from ai_agent_contracts.api import ControlArguments, ControlCommand

    await rt.supervisor.control(
        env.authority,
        ControlCommand(
            type="agent.start",
            request_id="activate-control",
            target="primary",
            scope=rt.supervisor.snapshot.scope,
            expected_version=rt.supervisor.snapshot.version,
            arguments=ControlArguments(autonomy="autonomous"),
        ),
    )
    app = FastAPI()
    install_access(
        app,
        rt.supervisor,
        BootstrapSettings(operator_origin=origin, operator_password_hash=configured_hash()),
        rt.access_ports(),
    )
    env.app, env.lab_scope, env.scenario = app, lab_scope, scenario
    env.grants = grants
    env.query = dict(
        zip(
            ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
            env.world.scope.partition,
            strict=True,
        )
    )

    async def start():
        current = rt.supervisor.snapshot
        command = {
            "type": "laboratory.start",
            "request_id": "start-lab-control",
            "target": "laboratory",
            "scope": current.scope.model_dump(mode="json"),
            "expected_version": current.version,
            "arguments": {
                "schema_id": "laboratory.start-args",
                "data": {
                    "scenario_id": scenario.scenario_id,
                    "scenario_version": scenario.version,
                    "lab_scope": lab_scope.model_dump(mode="json"),
                    "seed": "synthetic-control",
                    "mode": "mock",
                },
            },
        }
        response = await env.client.post("/api/v1/commands", json=command, headers=env.headers)
        assert response.status_code == 200, response.text
        stored = await rt.jobs.store.load(response.json()["operation_id"], current.scope)
        env.job = stored[0]
        return env.job

    env.start = start
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
        env.release.set()
        await rt.close()


def command(env, version, *, event=False, request="control-command"):
    return {
        "type": "laboratory.event" if event else "laboratory.stop",
        "request_id": request,
        "target": "laboratory",
        "scope": env.runtime.supervisor.snapshot.scope.model_dump(mode="json"),
        "expected_version": version,
        "arguments": {
            "schema_id": "laboratory.event-args" if event else "laboratory.stop-args",
            "data": {
                "operation_id": env.job.operation_id,
                "lab_scope": env.lab_scope.model_dump(mode="json"),
                **({"text": "Вымышленное наблюдение."} if event else {}),
            },
        },
    }
