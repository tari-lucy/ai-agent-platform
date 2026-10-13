"""Authenticated HTTP through the accepted JobOwner and real synthetic SQLite."""

import asyncio

import httpx
import pytest
from access_support import ORIGIN, WORKER_TOKEN, Harness
from ai_agent_application.jobs.owner import Claim
from ai_agent_contracts.access import ServiceClaimReceipt, ServiceLeaseReceipt
from ai_agent_contracts.api import ServiceLeaseCommand
from ai_agent_contracts.codec import from_wire, to_wire
from ai_agent_contracts.states import OperationState
from pydantic import TypeAdapter
from test_jobs import Fixture

from ai_agent_core.access import AccessPorts, WorkerGrant


async def setup(tmp_path, *, capabilities=None, worker_id="synthetic-worker"):
    f = await Fixture().setup(tmp_path)
    grant = WorkerGrant(
        worker_id, f.worker.partitions, frozenset(capabilities or {"synthetic.compute"})
    )
    h = await Harness().setup(
        fixture=f, ports=AccessPorts(jobs=f.owner, worker=grant, extensions=f.registry)
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app),
        base_url="http://core:8080",
        headers={"Authorization": "Bearer " + WORKER_TOKEN},
    )
    return f, h, client


async def post(client, f, command, action):
    return await client.post(
        "/internal/v1/jobs/" + command.target + "/" + action,
        content=to_wire(command, extensions=f.registry),
        headers={"Content-Type": "application/json"},
    )


def test_claim_duplicate_renew_result_and_stop_cancel_receipt(tmp_path):
    async def scenario():
        f, h, client = await setup(tmp_path)
        job, command, token = await f.queued()
        request = f.claim_request(job)
        response = await post(client, f, request, "claim")
        assert response.status_code == 200, response.text
        claimed = from_wire(
            TypeAdapter(ServiceClaimReceipt), response.content, extensions=f.registry
        )
        assert claimed.acquired and claimed.lease.worker_id == "synthetic-worker"
        duplicate = await post(client, f, request, "claim")
        assert duplicate.status_code == 200
        assert duplicate.json()["acquired"] is False
        assert duplicate.json()["lease"]["attempt_id"] == claimed.lease.attempt_id
        renew = ServiceLeaseCommand(
            type="job.renew",
            request_id="renew",
            scope=job.scope,
            target=job.operation_id,
            operation_id=job.operation_id,
            lease=claimed.lease,
        )
        response = await post(client, f, renew, "lease")
        assert response.status_code == 200
        assert response.json()["lease"]["attempt_id"] == claimed.lease.attempt_id
        lease = from_wire(TypeAdapter(ServiceLeaseReceipt), response.content).lease
        result = f.result_request(Claim(command, lease))
        response = await post(client, f, result, "result")
        assert response.status_code == 200
        record, _ = await f.store.load(job.operation_id, job.scope)
        assert record.status == OperationState.SUCCEEDED
        assert not token.cancelled.is_set()
        assert WORKER_TOKEN not in response.text
        next_job, next_command, next_token = await f.queued("second-operation")
        response = await post(client, f, f.claim_request(next_job), "claim")
        second = from_wire(
            TypeAdapter(ServiceClaimReceipt), response.content, extensions=f.registry
        )
        await h.login()
        stop = await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop())
        assert stop.status_code == 200 and stop.json()["state"] == "STOPPING"
        assert next_token.cancelled.is_set()
        cancel = ServiceLeaseCommand(
            type="job.cancel_receipt",
            request_id="actual-cancel",
            scope=next_job.scope,
            target=next_job.operation_id,
            operation_id=next_job.operation_id,
            lease=second.lease,
        )
        response = await post(client, f, cancel, "lease")
        assert response.status_code == 200
        assert h.supervisor.snapshot.state == "STOPPED"
        await client.aclose()
        await h.close()
        await f.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "wrong-token",
        "operator-cookie",
        "origin",
        "worker-cookie-mix",
        "foreign-scope",
        "foreign-target",
        "wrong-capability",
        "wrong-worker",
        "fake-attempt",
    ],
)
def test_service_credential_purpose_scope_capability_and_lease_are_enforced(tmp_path, case):
    async def scenario():
        f, h, client = await setup(
            tmp_path,
            capabilities={"foreign-capability"} if case == "wrong-capability" else None,
            worker_id="foreign-worker" if case == "wrong-worker" else "synthetic-worker",
        )
        job, command, token = await f.queued()
        request = f.claim_request(job)
        if case in ("wrong-worker", "fake-attempt"):
            actual = await f.owner.claim(f.worker, request)
            request = f.result_request(actual)
            if case == "fake-attempt":
                result = request.result.model_copy(update={"attempt_id": "foreign-attempt"})
                request = request.model_copy(update={"result": result})
        action = "result" if case in ("wrong-worker", "fake-attempt") else "claim"
        if case == "missing":
            client.headers.pop("Authorization")
        elif case == "wrong-token":
            client.headers["Authorization"] = "Bearer " + "a" * 64
        elif case == "operator-cookie":
            await h.login()
            client.headers.pop("Authorization")
            client.headers["Cookie"] = "ai_operator_session=" + h.cookie
        elif case == "origin":
            client.headers["Origin"] = ORIGIN
        elif case == "worker-cookie-mix":
            client.headers["Cookie"] = "ai_operator_session=" + "x" * 43
        elif case == "foreign-scope":
            request = request.model_copy(
                update={"scope": job.scope.model_copy(update={"persona_id": "foreign"})}
            )
        if case == "foreign-target":
            response = await client.post(
                "/internal/v1/jobs/foreign/claim",
                content=to_wire(request),
                headers={"Content-Type": "application/json"},
            )
        else:
            response = await post(client, f, request, action)
        assert response.status_code in (401, 403, 409), response.text
        assert WORKER_TOKEN not in response.text
        stored, _ = await f.store.load(job.operation_id, job.scope)
        assert stored.status == (
            OperationState.RUNNING
            if case in ("wrong-worker", "fake-attempt")
            else OperationState.QUEUED
        )
        await client.aclose()
        await h.close()
        await f.db.close()

    asyncio.run(scenario())


def test_empty_service_grants_and_unavailable_owner_fail_closed():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        assert (
            await h.client.get(
                "/api/v1/auth/session", headers={"Authorization": "Bearer " + WORKER_TOKEN}
            )
        ).status_code == 403
        # Configured token alone never grants a worker identity or wildcard rights.
        response = await h.client.post(
            "/internal/v1/jobs/unknown/claim",
            json={},
            headers={"Authorization": "Bearer " + WORKER_TOKEN},
        )
        assert response.status_code == 403  # browser cookie is not a service identity
        h.client.cookies.clear()
        response = await h.client.post(
            "/internal/v1/jobs/unknown/claim",
            json={},
            headers={"Authorization": "Bearer " + WORKER_TOKEN},
        )
        assert response.status_code == 401
        await h.close()

    asyncio.run(scenario())


def test_bootstrap_factory_uses_same_supervisor_and_rejects_foreign_owner(tmp_path):
    from ai_agent_adapters.configuration import BootstrapSettings, ConfigurationError
    from ai_agent_application.jobs.owner import JobOwner
    from fastapi import FastAPI

    from ai_agent_core.access import install_access
    from ai_agent_core.bootstrap import create_app

    async def scenario():
        f, h, client = await setup(tmp_path)
        with pytest.raises(ValueError, match="ACCESS_JOB_OWNER"):
            unrelated = await Harness().setup()
            install_access(FastAPI(), unrelated.supervisor, h.settings, AccessPorts(jobs=f.owner))
        await unrelated.close()
        built = []

        def factory(supervisor):
            owner = JobOwner(f.store, supervisor, f.policy, extensions=f.registry)
            built.append(owner)
            return AccessPorts(jobs=owner, extensions=f.registry)

        app = create_app(BootstrapSettings(memory_scope="test"), access_factory=factory)
        assert built[0].supervisor is app.state.supervisor
        assert app.state.supervisor.snapshot.state == "STOPPED"
        with pytest.raises(ConfigurationError):
            create_app(BootstrapSettings(), access_factory=factory, access_ports=AccessPorts())
        await client.aclose()
        await h.close()
        await f.db.close()

    asyncio.run(scenario())
