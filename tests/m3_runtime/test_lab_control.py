"""Real HTTP/SQLite laboratory event, exact job cancellation, replay and fences."""

import asyncio
from dataclasses import replace

from ai_agent_application.laboratory.lab import MEMORY
from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import OperationState
from composition_support import run
from lab_control_support import command, prepared

from ai_agent_core.m3_composition.state import PRINCIPALS


def test_event_is_one_private_test_source_and_cancel_has_factual_receipt(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path, monkeypatch) as env:
            job = await env.start()
            task = asyncio.create_task(env.runtime.execute(job))
            await asyncio.wait_for(env.entered.wait(), 10)
            page = await env.client.get("/api/v1/laboratory-runs", params=env.query)
            assert page.status_code == 200, page.text
            row = page.json()["items"][0]
            assert row["ready"] and row["status"] == "RUNNING"
            event = command(env, row["version"], event=True)
            response = await env.client.post("/api/v1/commands", json=event, headers=env.headers)
            assert response.status_code == 200, response.text
            source = await env.world.store.repository(Source, MEMORY, env.lab_scope).get(
                response.json()["resource_id"]
            )
            assert source.content == "Вымышленное наблюдение." and not source.api_transfer_allowed
            repeated = await env.client.post("/api/v1/commands", json=event, headers=env.headers)
            assert repeated.status_code == 200 and repeated.json()["resource_id"] == source.id
            assert env.runtime.lab_control.live[job.id].count == 1
            assert not await env.world.store.repository(
                Source, PRINCIPALS[Owner.MEMORY], env.world.scope
            ).get(source.id)
            halt = command(env, row["version"], request="halt")
            result = await env.client.post("/api/v1/commands", json=halt, headers=env.headers)
            assert result.status_code == 200, result.text
            settled = await asyncio.wait_for(task, 10)
            assert settled.status == OperationState.CANCELLED
            assert env.runtime.supervisor.snapshot.state.value == "ACTIVE"
            late = await env.client.post(
                "/api/v1/commands",
                json=command(env, settled.version, event=True, request="late"),
                headers=env.headers,
            )
            assert late.status_code in (409, 403)
            assert not env.world.provider.calls

    run(scenario())


def test_queued_cancel_replay_and_wrong_cas_do_not_cancel_other_job(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path, monkeypatch) as env:
            job = await env.start()
            wrong = command(env, job.version + 1)
            refused = await env.client.post("/api/v1/commands", json=wrong, headers=env.headers)
            assert refused.status_code == 409
            assert (await env.runtime.jobs.store.load(job.id, job.scope))[
                0
            ].status == OperationState.QUEUED
            halt = command(env, job.version, request="queued-cancel")
            assert (
                await env.client.post("/api/v1/commands", json=halt, headers=env.headers)
            ).status_code == 200
            assert (
                await env.client.post("/api/v1/commands", json=halt, headers=env.headers)
            ).status_code == 200
            assert (await env.runtime.jobs.store.load(job.id, job.scope))[
                0
            ].status == OperationState.CANCELLED

    run(scenario())


def test_revoked_job_api_access_closes_control(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path, monkeypatch) as env:
            job = await env.start()
            await env.world.store.provision(
                env.world.scope,
                tuple(
                    replace(g, api_transfer=False) if g.principal.id == "operator" else g
                    for g in env.grants
                ),
            )
            result = await env.client.post(
                "/api/v1/commands", json=command(env, job.version), headers=env.headers
            )
            assert result.status_code == 403
            assert (await env.runtime.jobs.store.load(job.id, job.scope))[
                0
            ].status == OperationState.QUEUED

    run(scenario())


def test_event_limit_and_report_records_observation_refs_without_copying_text(
    tmp_path, monkeypatch
):
    async def scenario():
        async with prepared(tmp_path, monkeypatch) as env:
            job = await env.start()
            task = asyncio.create_task(env.runtime.execute(job))
            await asyncio.wait_for(env.entered.wait(), 10)
            page = (await env.client.get("/api/v1/laboratory-runs", params=env.query)).json()
            version = page["items"][0]["version"]
            for i in range(32):
                response = await env.client.post(
                    "/api/v1/commands",
                    json=command(env, version, event=True, request="event-" + str(i)),
                    headers=env.headers,
                )
                assert response.status_code == 200, response.text
            refused = await env.client.post(
                "/api/v1/commands",
                json=command(env, version, event=True, request="overflow"),
                headers=env.headers,
            )
            assert refused.status_code == 429
            assert env.runtime.lab_control.live[job.id].count == 32
            env.release.set()
            settled = await asyncio.wait_for(task, 10)
            assert settled.status == OperationState.SUCCEEDED
            report = env.runtime.lab.reports[-1]
            events = [row for row in report.log if row["kind"] == "operator_observation"]
            assert len(events) == 32
            assert "Вымышленное наблюдение." not in report.to_json()
            assert not env.world.provider.calls

    run(scenario())


def test_foreign_test_and_stale_run_never_revoke_the_job(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path, monkeypatch) as env:
            job = await env.start()
            foreign = command(env, job.version)
            foreign["arguments"]["data"]["lab_scope"]["memory_space_id"] = "ungranted-test"
            result = await env.client.post("/api/v1/commands", json=foreign, headers=env.headers)
            assert result.status_code == 403
            stale = command(env, job.version, request="old-run")
            stale["scope"]["run_id"] = "old-run"
            result = await env.client.post("/api/v1/commands", json=stale, headers=env.headers)
            assert result.status_code in (403, 409)
            assert (await env.runtime.jobs.store.load(job.id, job.scope))[
                0
            ].status == OperationState.QUEUED

    run(scenario())
