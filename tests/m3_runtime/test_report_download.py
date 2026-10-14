"""Real ASGI/SQLite download; no full lab scenario or model execution."""

import hashlib
from dataclasses import replace

import pytest
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.memory import Source
from ai_agent_contracts.workspace import Artifact
from composition_support import run
from report_support import prepared

from ai_agent_core.m3_composition.state import PRINCIPALS, put


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
                env.world.store,
                env.runtime.registry,
                Owner.LABORATORY,
                env.scope,
                "stale-report-ref",
                env.report.model_copy(update={"version": 2, "report_artifact": ref}),
                1,
                (RecordAccess("operator", api_transfer=True),),
            )
            response = await env.client.get(env.url, params=env.query)
            assert response.status_code == 409
            assert "synthetic full report" not in response.text

    run(scenario())
