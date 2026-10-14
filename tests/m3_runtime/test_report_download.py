"""Real ASGI/SQLite download; no full lab scenario or model execution."""

import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_application.laboratory.report import Finding, ScenarioReport
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.memory import Source
from ai_agent_contracts.workspace import Artifact, LaboratoryReport
from composition_support import run, setup
from fastapi import FastAPI

from ai_agent_core.access import install_access
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import PRINCIPALS, put


@asynccontextmanager
async def prepared(tmp_path, *, missing=None):
    env = await setup(tmp_path, start=False)
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
    app = FastAPI()
    install_access(
        app,
        rt.supervisor,
        BootstrapSettings(operator_origin=ORIGIN, operator_password_hash=configured_hash()),
        rt.access_ports(),
    )
    env.query = dict(
        zip(
            ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
            scope.partition,
            strict=True,
        )
    )
    env.scope, env.report, env.reports, env.grants = scope, report, reports, grants
    env.url = "/api/v1/laboratory-reports/report-1/content"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            assert (await client.get(env.url, params=env.query)).status_code == 401
            login = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert login.status_code == 200
            env.client = client
            env.headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
            yield env
    finally:
        await rt.close()


def test_download_exact_bytes_and_bounded_safe_http_surface(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            client = env.client
            response = await client.get(env.url, params=env.query)
            assert response.status_code == 200, response.text
            assert response.json()["log"] == [{"detail": "synthetic full report"}]
            assert response.json()["mock"] is True
            ref = env.report.report_artifact
            assert len(response.content) == ref.size_bytes
            assert hashlib.sha256(response.content).hexdigest() == ref.checksum
            assert response.headers["etag"] == '"' + ref.checksum + '"'
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert (
                response.headers["content-disposition"]
                == 'attachment; filename="laboratory-report.json"'
            )
            assert (
                await client.get(
                    env.url, params=env.query, headers={"Origin": "https://foreign.invalid"}
                )
            ).status_code == 403
            assert (
                await client.get(env.url, params={**env.query, "memory_space_id": "foreign"})
            ).status_code == 403
            assert (
                await client.get(env.url, params={**env.query, "cursor": "unsafe"})
            ).status_code == 400
            assert (
                await client.get(env.url.replace("report-1", "unknown"), params=env.query)
            ).status_code == 403
            assert not env.world.provider.calls

    run(scenario())


@pytest.mark.parametrize(
    "missing",
    [
        "source-rights",
        "source-acl",
        "artifact-acl",
        "report-acl",
        "catalogue-grant",
        "memory-grant",
        "laboratory-grant",
        "owner",
    ],
)
def test_download_needs_each_current_permission_and_configured_owner(tmp_path, missing):
    async def scenario():
        async with prepared(tmp_path, missing=missing) as env:
            response = await env.client.get(env.url, params=env.query)
            assert response.status_code == (503 if missing == "owner" else 403)
            assert "synthetic full report" not in response.text

    run(scenario())


def test_corruption_missing_file_and_expiry_never_release_content(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            artifact = await env.world.store.repository(
                Artifact, PRINCIPALS[Owner.CATALOGUE], env.scope
            ).get(env.report.report_artifact.artifact_id)
            path = env.reports._path(env.scope, artifact.provenance_source.source_id)
            body = path.read_bytes()
            path.write_bytes(b" " + body[1:])
            assert (await env.client.get(env.url, params=env.query)).status_code == 409
            path.write_bytes(b"x" * (env.reports.max_bytes + 1))
            assert (await env.client.get(env.url, params=env.query)).status_code == 409
            path.unlink()
            assert (await env.client.get(env.url, params=env.query)).status_code == 409
            path.write_bytes(body)
            env.world.clock.advance(seconds=11)
            expired = await env.client.get(env.url, params=env.query)
            assert expired.status_code == 403
            assert "synthetic full report" not in expired.text

    run(scenario())


@pytest.mark.parametrize("revocation", ["source", "report", "session"])
def test_revoke_during_io_is_rechecked_before_return(tmp_path, monkeypatch, revocation):
    async def scenario():
        async with prepared(tmp_path) as env:
            store = env.world.store
            if revocation == "source":
                original = store.repository
                seen = 0

                def repository(model, principal, scope):
                    repo = original(model, principal, scope)
                    get = repo.get

                    async def checked(*args, **kwargs):
                        nonlocal seen
                        if model is Artifact and principal.id == "operator":
                            seen += 1
                            if seen == 2:
                                await store.provision(
                                    env.scope,
                                    tuple(
                                        replace(g, api_transfer=False)
                                        if g.principal.id == "operator" and g.owner == Owner.MEMORY
                                        else g
                                        for g in env.grants
                                    ),
                                )
                        return await get(*args, **kwargs)

                    repo.get = checked
                    return repo

                monkeypatch.setattr(store, "repository", repository)
            else:
                original_read = env.reports.read

                async def read(*args, **kwargs):
                    result = await original_read(*args, **kwargs)
                    if revocation == "session":
                        assert (
                            await env.client.post(
                                "/api/v1/auth/logout", json={}, headers=env.headers
                            )
                        ).status_code == 204
                    else:
                        await put(
                            store,
                            env.runtime.registry,
                            Owner.LABORATORY,
                            env.scope,
                            "revoke-report",
                            env.report.model_copy(update={"version": 2}),
                            1,
                            (),
                        )
                    return result

                monkeypatch.setattr(env.reports, "read", read)
            response = await env.client.get(env.url, params=env.query)
            assert response.status_code in (401, 403)
            assert "synthetic full report" not in response.text

    run(scenario())


def test_deleted_source_closes_report_even_while_its_file_still_exists(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            store, scope = env.world.store, env.scope
            artifact = await store.repository(Artifact, PRINCIPALS[Owner.CATALOGUE], scope).get(
                env.report.report_artifact.artifact_id
            )
            source = await store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).get(
                artifact.provenance_source.source_id
            )
            await put(
                store,
                env.runtime.registry,
                Owner.MEMORY,
                scope,
                "delete-report-source",
                source.model_copy(
                    update={
                        "version": 2,
                        "availability": "deleted",
                        "content": None,
                        "artifact": None,
                    }
                ),
                1,
                (),
            )
            assert env.reports._path(scope, source.id).exists()
            response = await env.client.get(env.url, params=env.query)
            assert response.status_code == 403
            assert "synthetic full report" not in response.text

    run(scenario())


def test_stale_artifact_version_never_falls_back_to_existing_bytes(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            ref = env.report.report_artifact.model_copy(update={"version": 2})
            await put(
                env.world.store, env.runtime.registry, Owner.LABORATORY, env.scope,
                "stale-report-ref",
                env.report.model_copy(update={"version": 2, "report_artifact": ref}),
                1, (RecordAccess("operator", api_transfer=True),),
            )
            response = await env.client.get(env.url, params=env.query)
            assert response.status_code == 409
            assert "synthetic full report" not in response.text

    run(scenario())
