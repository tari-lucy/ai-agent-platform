"""Reservations use the existing SQLite owner journal and an explicit authorised pool."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_application.laboratory import LabRunner, lab_grants
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import Permission
from ai_agent_contracts.memory import Retention, Source
from ai_agent_contracts.states import Provenance
from ai_agent_contracts.workspace import LaboratoryScopeReservation
from composition_support import run, setup
from fastapi import FastAPI

from ai_agent_core.access import install_access
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import PRINCIPALS, put


async def fixture(tmp_path, *, restricted=False, occupied=False):
    env = await setup(tmp_path, start=False)
    rt = env.runtime
    children = tuple(
        env.world.scope.model_copy(update={"memory_mode": "test", "memory_space_id": f"pool-{n}"})
        for n in (1, 2)
    )
    for child in children:
        await rt.store.provision(child, env.grants + lab_grants())
    rt.scopes += children

    def forbidden(*args):
        raise AssertionError("Preparation must not call a factory or model")

    rt.lab = LabRunner(rt.store, forbidden, forbidden)
    rt.reports = Reports(tmp_path / "reports", rt.store, rt.registry, clock=env.world.clock)
    if occupied:
        now = env.world.clock()
        old = Source(
            id="removed",
            source_id="removed",
            scope=children[0],
            version=1,
            created_at=now,
            updated_at=now,
            provenance=Provenance.SIMULATION,
            source_type="synthetic",
            adapter_id="mock",
            adapter_version="1",
            occurred_at=now,
            registered_at=now,
            content=None,
            artifact=None,
            retention=Retention(policy_id="fixture", expires_at=None),
            availability="deleted",
            read_allowed=False,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        await put(rt.store, rt.registry, Owner.MEMORY, children[0], "removed-source", old, None)
    app = FastAPI()
    ports = rt.access_ports()
    if restricted:
        ports = replace(ports, scopes=(env.world.scope,))
    install_access(
        app,
        rt.supervisor,
        BootstrapSettings(operator_origin=ORIGIN, operator_password_hash=configured_hash()),
        ports,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    login = await client.post(
        "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    env.client = client
    env.headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
    env.command = {
        "type": "laboratory.create",
        "request_id": "prepare-1",
        "target": "laboratory",
        "scope": env.world.scope.model_dump(mode="json"),
        "arguments": {
            "schema_id": "laboratory.create-args",
            "data": {"persona_id": env.world.scope.persona_id},
        },
    }
    env.children = children
    return env


def test_reservations_are_distinct_replayed_and_durable(tmp_path):
    async def scenario():
        env = await fixture(tmp_path)
        try:
            first = await env.client.post("/api/v1/commands", json=env.command, headers=env.headers)
            assert first.status_code == 200, first.text
            assert first.json()["resource_id"] == "pool-1"
            replay = await env.client.post(
                "/api/v1/commands", json=env.command, headers=env.headers
            )
            assert replay.json() == first.json()
            other = await env.client.post(
                "/api/v1/commands",
                json={**env.command, "request_id": "prepare-2"},
                headers=env.headers,
            )
            assert other.status_code == 200 and other.json()["resource_id"] == "pool-2"
            # Dispose/reopen the actual SQLite connection pool; reservations are not a Python list.
            await env.world.store.close()
            replay = await env.client.post(
                "/api/v1/commands", json=env.command, headers=env.headers
            )
            assert replay.json() == first.json()
            full = await env.client.post(
                "/api/v1/commands",
                json={**env.command, "request_id": "prepare-3"},
                headers=env.headers,
            )
            assert full.status_code == 429
            records = await env.world.store.repository(
                LaboratoryScopeReservation, PRINCIPALS[Owner.LABORATORY], env.world.scope
            ).list()
            assert len(records) == 2
            assert all([await env.world.store.empty_test_scope(child) for child in env.children])
            assert not env.world.provider.calls
        finally:
            await env.client.aclose()
            await env.runtime.close()

    run(scenario())


def test_preparation_skips_deleted_or_hidden_data_in_a_scope(tmp_path):
    async def scenario():
        env = await fixture(tmp_path, occupied=True)
        try:
            result = await env.client.post(
                "/api/v1/commands", json=env.command, headers=env.headers
            )
            assert result.status_code == 200 and result.json()["resource_id"] == "pool-2"
        finally:
            await env.client.aclose()
            await env.runtime.close()

    run(scenario())


def test_preparation_never_grants_a_scope_outside_the_session(tmp_path):
    async def scenario():
        env = await fixture(tmp_path, restricted=True)
        try:
            result = await env.client.post(
                "/api/v1/commands", json=env.command, headers=env.headers
            )
            assert result.status_code == 429
            assert (await env.client.get("/api/v1/auth/session")).json()["scopes"] == [
                env.world.scope.model_dump(mode="json")
            ]
        finally:
            await env.client.aclose()
            await env.runtime.close()

    run(scenario())


def test_concurrent_requests_cannot_reserve_the_same_scope(tmp_path):
    async def scenario():
        env = await fixture(tmp_path)
        try:
            results = await asyncio.gather(
                *(
                    env.client.post(
                        "/api/v1/commands",
                        json={**env.command, "request_id": f"parallel-{n}"},
                        headers=env.headers,
                    )
                    for n in (1, 2)
                )
            )
            assert all(r.status_code == 200 for r in results)
            assert {r.json()["resource_id"] for r in results} == {"pool-1", "pool-2"}
        finally:
            await env.client.aclose()
            await env.runtime.close()

    run(scenario())


@pytest.mark.parametrize("blocked", ["permission", "reports", "runner"])
def test_preparation_needs_existing_permission_and_explicit_owners(tmp_path, blocked):
    async def scenario():
        env = await fixture(tmp_path)
        try:
            rt = env.runtime
            if blocked == "reports":
                rt.reports = None
            elif blocked == "runner":
                rt.lab = None
            else:
                old = await rt.store.repository(
                    Permission, PRINCIPALS[Owner.WORKSPACE], env.world.scope
                ).get("permit-laboratory")
                await put(
                    rt.store,
                    rt.registry,
                    Owner.WORKSPACE,
                    env.world.scope,
                    "revoke-lab-permit",
                    old.model_copy(
                        update={"version": old.version + 1, "revoked_at": env.world.clock()}
                    ),
                    old.version,
                )
            result = await env.client.post(
                "/api/v1/commands", json=env.command, headers=env.headers
            )
            assert result.status_code == (403 if blocked == "permission" else 503)
            assert not await rt.store.repository(
                LaboratoryScopeReservation, PRINCIPALS[Owner.LABORATORY], env.world.scope
            ).list()
        finally:
            await env.client.aclose()
            await env.runtime.close()

    run(scenario())
