"""Production host/HTTP, synthetic transport and prices; no working data or paid calls."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from ai_agent_adapters.llm import OpenAICompatibleProvider
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.llm import LLMAssignment
from usage_support import command, report

from ai_agent_core.local_host import live_budget, provider, rights
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import LiveConfig, load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.serve import build
from ai_agent_core.m3_composition.state import PRINCIPALS


@asynccontextmanager
async def world(tmp_path, monkeypatch, *, recovery=False, restart=False):
    root = tmp_path / "host"
    host = paths(root)
    if not restart:
        await initialise(
            root,
            profile_markdown=REFERENCE_PROFILE,
            operator_controls=True,
            live=LiveConfig(
                model_id="fixture-model",
                attempt_limit=300,
                attempt_period="utc_day",
                memory_processing=True,
            ),
        )
        if recovery:
            await enable_recovery(
                root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
            )
    config = load_config(host)
    binding = tmp_path / "synthetic.env"
    binding.write_text(
        "AI_PLATFORM_LLM_ENDPOINT=https://fixture.invalid/v1\nAI_PLATFORM_LLM_API_KEY=fictional\n"
    )

    def reject(request):
        raise AssertionError("No LLM call is needed for settings")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as wire:
        monkeypatch.setattr(
            provider,
            "OpenAICompatibleProvider",
            lambda settings: OpenAICompatibleProvider(settings, wire),
        )
        app, close = build(host, config, provider="live", env_file=binding)
        rt = app.state.runtime
        try:
            async with app.router.lifespan_context(app):
                if rt.maintenance is not None:
                    rt.maintenance.stop.set()
                    await rt.maintenance.task
                origin = "http://127.0.0.1:8765"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=origin
                ) as http:
                    login = await http.post(
                        "/api/v1/auth/login",
                        json={"password": host.password.read_text().strip()},
                        headers={"Origin": origin},
                    )
                    assert login.status_code == 200
                    scope = rt.supervisor.snapshot.scope
                    params = {
                        key: value
                        for key, value in scope.model_dump(mode="json").items()
                        if key in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
                    }
                    yield SimpleNamespace(
                        rt=rt,
                        http=http,
                        host=host,
                        scope=scope,
                        params=params,
                        headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
                    )
        finally:
            close()


async def post(env, body):
    return await env.http.post("/api/v1/commands", headers=env.headers, json=body)


def cap(env, limit, version):
    return dict(
        type="llm.limits",
        target="api-limits",
        request_id=uuid4().hex,
        scope=env.params,
        expected_version=version,
        arguments=dict(schema_id="llm.limits-args", data=dict(attempt_limit=limit)),
    )


async def seed_attempt(env, identifier, *, unknown=False):
    rt = env.rt
    ledger = UsageLedger(
        rt.store,
        PRINCIPALS[Owner.LLM],
        rt.registry,
        clock=rt.supervisor.clock.utc,
        reader_access=rt.readers,
    )
    assignments = await rt.store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], env.scope).list(
        limit=50
    )
    assignment = next(row for row in assignments if row.role_id == "character")
    payload = replace(
        report(identifier, at=rt.supervisor.clock.utc()),
        connection_id=assignment.connection_id,
        requested_model_id=assignment.model_id,
        assignment_id=assignment.id,
        assignment_version=assignment.version,
    )
    if unknown:
        from usage_support import UNKNOWN

        payload = replace(payload, usage=UNKNOWN)
    await ledger.record_attempt(command(env.scope, uuid4().hex), payload)
    return ledger


def test_cap_cas_replay_reserves_usage_restart_and_ledger_rmw(tmp_path, monkeypatch):
    async def scenario():
        async with world(tmp_path, monkeypatch, recovery=True) as env:
            await seed_attempt(env, "already-spent")
            projection = await env.http.get("/api/v1/api-limits", params=env.params)
            assert projection.status_code == 200, projection.text
            assert (
                projection.json()["attempt_limit"] == 300
                and projection.json()["minimum_limit"] == 9
            )
            request = cap(env, 320, 1)
            assert (await env.http.post("/api/v1/commands", json=request)).status_code == 403
            assert (await post(env, cap(env, 8, 1))).json()["code"] == "INVALID_ARGUMENTS"
            # The deletion-ledger owner also updates host.json; both RMWs must survive.
            done, _ = await asyncio.gather(
                post(env, request),
                env.rt.maintenance.ledger.block(
                    env.scope, "synthetic-deleted", 1, "expired", env.rt.supervisor.clock.utc()
                ),
            )
            assert done.status_code == 200, done.text
            assert (await post(env, request)).json() == done.json()
            assert (await post(env, cap(env, 340, 1))).json()["code"] == "VERSION_CONFLICT"
            assert (
                await post(
                    env,
                    {
                        **request,
                        "arguments": {**request["arguments"], "data": {"attempt_limit": 321}},
                    },
                )
            ).json()["code"] == "IDEMPOTENCY_CONFLICT"
            persisted = load_config(env.host)
            assert persisted.recovery.ledger_floor == 1
            assert persisted.live.attempt_limit == env.rt.private_api_budget.max_attempts == 320
            status = (await env.http.get("/api/v1/private-api-status", params=env.params)).json()
            assert status["attempts_used"] == 1 and status["attempt_limit"] == 320
            test_scope = {
                **env.params,
                "memory_mode": "test",
                "memory_space_id": persisted.test_space_ids[0],
            }
            assert (await env.http.get("/api/v1/api-limits", params=test_scope)).status_code == 403
            run = env.rt.supervisor.snapshot
            started = await post(
                env,
                dict(
                    type="agent.start",
                    request_id="start",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                    expected_version=run.version,
                    arguments=dict(autonomy="observation"),
                ),
            )
            assert started.status_code == 200, started.text
            assert (await post(env, cap(env, 340, 2))).json()["code"] == "RESOURCE_BUSY"
        async with world(tmp_path, monkeypatch, restart=True) as env:
            assert env.rt.supervisor.snapshot.state.value == "STOPPED"
            assert env.rt.private_api_budget.max_attempts == 320
            status = (await env.http.get("/api/v1/private-api-status", params=env.params)).json()
            assert status["attempts_used"] == 1
            assert (await post(env, request)).json() == done.json()

    asyncio.run(scenario())


def test_cap_recovers_file_commit_without_applying_twice(tmp_path, monkeypatch):
    async def scenario():
        async with world(tmp_path, monkeypatch) as env:
            save = live_budget.save_config

            def interrupted(*args):
                save(*args)
                raise OSError("synthetic acknowledgement loss")

            monkeypatch.setattr(live_budget, "save_config", interrupted)
            request = cap(env, 350, 1)
            assert (await post(env, request)).json()["code"] == "OUTCOME_UNKNOWN"
            assert load_config(env.host).live.attempt_limit == 350
            assert env.rt.private_api_budget.max_attempts == 300
            monkeypatch.setattr(live_budget, "save_config", save)
            done = await post(env, request)
            assert done.status_code == 200, done.text
            assert done.json()["status"] == "completed"
            assert load_config(env.host).live.budget_version == 2
            assert env.rt.private_api_budget.max_attempts == 350
            # A later valid edit must not prevent replay of the earlier receipt.
            assert (await post(env, cap(env, 360, 2))).status_code == 200
            assert (await post(env, request)).json() == done.json()
            assert env.rt.private_api_budget.max_attempts == 360

    asyncio.run(scenario())


def test_tariff_cas_receipts_pinned_prices_unknown_usage_and_access(tmp_path, monkeypatch):
    async def scenario():
        async with world(tmp_path, monkeypatch) as env:

            def tariff(version, rate):
                return dict(
                    type="tariff.create",
                    request_id=uuid4().hex,
                    target="conn-live",
                    scope=env.params,
                    arguments=dict(
                        schema_id="tariff.create-args",
                        data=dict(
                            connection_id="conn-live",
                            model_id="fixture-model",
                            previous_version=version,
                            currency="USD",
                            unit_tokens=1_000_000,
                            input_rate=rate,
                            output_rate="1",
                            effective_at=(
                                env.rt.supervisor.clock.utc() - timedelta(days=1)
                            ).isoformat(),
                        ),
                    ),
                )

            first = tariff(0, "0.20")
            done = await post(env, first)
            assert done.status_code == 200, done.text
            assert done.json()["version"] == 1
            ledger = await seed_attempt(env, "old-price")
            cost = await ledger.calculate(command(env.scope, "old-cost"), "old-price")
            assert cost.amount == "0.0007" and cost.tariff_version == 1
            assert (await post(env, tariff(1, "0.40"))).json()["version"] == 2
            assert (await post(env, first)).json() == done.json()
            assert (await post(env, tariff(0, "0.90"))).json()["code"] == "VERSION_CONFLICT"
            assert (await post(env, {**first, "arguments": tariff(0, "9")["arguments"]})).json()[
                "code"
            ] == "IDEMPOTENCY_CONFLICT"
            await seed_attempt(env, "new-price")
            newer = await ledger.calculate(command(env.scope, "new-cost"), "new-price")
            assert newer.amount == "0.0009" and newer.tariff_version == 2
            assert await ledger.cost_of(env.scope, "old-price") == cost
            await seed_attempt(env, "unknown-usage", unknown=True)
            unknown = await ledger.calculate(command(env.scope, "unknown-cost"), "unknown-usage")
            assert unknown.amount is None and unknown.completeness == "unknown"
            # Removing this operator's LLM access must close both reads and mutations.
            await env.rt.store.provision(
                env.scope,
                tuple(
                    grant
                    for grant in rights.work_grants(operator_controls=True)
                    if not (grant.principal.id == "operator" and grant.owner is Owner.LLM)
                ),
            )
            assert (await post(env, tariff(2, "0.80"))).status_code == 403
            assert (
                await ledger.tariff_at(
                    env.scope, "conn-live", "fixture-model", env.rt.supervisor.clock.utc()
                )
            ).version == 2

    asyncio.run(scenario())


def test_offline_metadata_preparation_is_explicit_idempotent_and_not_permissions(tmp_path):
    from ai_agent_adapters.storage.database import SQLiteStore
    from ai_agent_application.storage import Principal, RecordAccess
    from ai_agent_contracts.api import Permission
    from ai_agent_contracts.llm import Connection

    from ai_agent_core.local_host.llm_panel import prepare_llm_panel
    from ai_agent_core.m3_composition import new_registry
    from ai_agent_core.m3_composition.state import put

    async def scenario():
        root = tmp_path / "legacy-host"
        config = await initialise(root, profile_markdown=REFERENCE_PROFILE, operator_controls=True)
        host = paths(root)
        from ai_agent_core.local_host.init import work_scope

        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        try:
            old = await store.repository(Connection, PRINCIPALS[Owner.WORKSPACE], scope).get(
                "conn-mock"
            )
            # Exact legacy ACL; no operator access. The command must not change its binding.
            await put(
                store,
                registry,
                Owner.WORKSPACE,
                scope,
                "synthetic-legacy",
                old.model_copy(update={"version": 2}),
                1,
                (RecordAccess(PRINCIPALS[Owner.LLM].id),),
            )
            before = await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list(
                limit=50
            )
            assert (
                await store.repository(Connection, Principal("operator", "operator"), scope).get(
                    "conn-mock", purpose="api_transfer"
                )
                is None
            )
        finally:
            await store.close()
        await prepare_llm_panel(root)
        await prepare_llm_panel(root)
        store = SQLiteStore(host.database, extensions=registry)
        try:
            current = await store.repository(
                Connection, Principal("operator", "operator"), scope
            ).get("conn-mock", purpose="api_transfer")
            assert current.version == 3
            assert current.model_dump(exclude={"version", "updated_at"}) == old.model_dump(
                exclude={"version", "updated_at"}
            )
            assert (
                await store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list(
                    limit=50
                )
                == before
            )
            assert load_config(host) == config
        finally:
            await store.close()

    asyncio.run(scenario())

