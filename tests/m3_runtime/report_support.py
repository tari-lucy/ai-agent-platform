"""Real ASGI/SQLite download; no full lab scenario or model execution."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_application.laboratory.report import Finding, ScenarioReport
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import Permission
from ai_agent_contracts.memory import Source
from ai_agent_contracts.workspace import Artifact, LaboratoryReport
from composition_support import setup
from fastapi import FastAPI

from ai_agent_core.access import install_access
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import PRINCIPALS, put


@asynccontextmanager
async def prepared(tmp_path, *, missing=None, origin=ORIGIN, realtime=False, configure=None):
    env = await setup(tmp_path, start=False)
    if realtime:
        env.initial_permissions = await env.world.store.repository(
            Permission, PRINCIPALS[Owner.WORKSPACE], env.world.scope
        ).list(limit=200)
        env.world.clock.now = datetime.now(UTC)
    rt, store = env.runtime, env.world.store
    scope = env.world.scope.model_copy(
        update={"memory_mode": "test", "memory_space_id": "report-test"}
    )
    operator = Principal("operator", "operator")
    owners = (Owner.MEMORY, Owner.CATALOGUE, Owner.LABORATORY)
    grants = env.grants + tuple(
        Grant(operator, owner, read=True, api_transfer=missing != owner.value + "-grant")
        for owner in owners
    )
    await store.provision(scope, grants)
    rt.scopes += (scope,)
    readers = (
        RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),
        RecordAccess("operator", api_transfer=missing != "artifact-acl"),
    )
    reports = Reports(
        tmp_path / "reports",
        store,
        rt.registry,
        clock=env.world.clock,
        readers=readers,
        **({} if missing == "source-rights" else {"api_transfer_allowed": True}),
        ttl=timedelta(seconds=10),
    )
    now = env.world.clock()
    full = ScenarioReport(
        "synthetic",
        1,
        "seed",
        now,
        now,
        "passed",
        (Finding("STOP", True),),
        ({"detail": "synthetic full report"},),
        0,
        source_refs=(),
    )
    reference = await reports.save(scope, full)
    if missing == "source-acl":
        artifact = await store.repository(Artifact, PRINCIPALS[Owner.CATALOGUE], scope).get(
            reference.artifact_id
        )
        source = await store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).get(
            artifact.provenance_source.source_id
        )
        await put(
            store,
            rt.registry,
            Owner.MEMORY,
            scope,
            "revoke-source-acl",
            source.model_copy(update={"version": 2}),
            1,
            (RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),),
        )
    report = LaboratoryReport(
        id="report-1",
        scope=scope,
        version=1,
        created_at=now,
        updated_at=now,
        scenario_id="synthetic",
        scenario_version=1,
        operation_id="lab-op",
        status="passed",
        summary="Synthetic",
        report_artifact=reference,
    )
    await put(
        store,
        rt.registry,
        Owner.LABORATORY,
        scope,
        "save-report",
        report,
        None,
        (RecordAccess("operator", api_transfer=missing != "report-acl"),),
    )
    rt.reports = None if missing == "owner" else reports
    env.scope, env.report, env.reports, env.grants = scope, report, reports, grants
    if configure is not None:
        await configure(env)
    app = FastAPI()
    install_access(
        app,
        rt.supervisor,
        BootstrapSettings(operator_origin=origin, operator_password_hash=configured_hash()),
        rt.access_ports(),
    )
    env.query = dict(
        zip(
            ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
            scope.partition,
            strict=True,
        )
    )
    env.app = app
    env.scope, env.report, env.reports, env.grants = scope, report, reports, grants
    env.url = "/api/v1/laboratory-reports/report-1/content"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as client:
            assert (await client.get(env.url, params=env.query)).status_code == 401
            login = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": origin}
            )
            assert login.status_code == 200
            env.client = client
            env.headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
            yield env
    finally:
        await rt.close()
