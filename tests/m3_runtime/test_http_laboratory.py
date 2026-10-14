"""ASGI lifespan/auth boundary and a queued mock laboratory with a full report."""

import json

import httpx
import pytest
from access_support import ORIGIN, PASSWORD, configured_hash
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory import LabRunner, VirtualClock, lab_grants
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.states import OperationState, RunState
from ai_agent_contracts.workspace import LaboratoryReport
from composition_support import run, setup

from ai_agent_core.m3_composition.app import create_m3_app
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.schemas import TaskInput
from ai_agent_core.m3_composition.state import PRINCIPALS


def test_app_factory_lifespan_auth_gm_cas_and_direct_stop(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        await env.runtime.close()
        scope = env.world.scope
        settings = BootstrapSettings(
            workspace_id=scope.workspace_id,
            persona_id=scope.persona_id,
            operator_origin=ORIGIN,
            operator_password_hash=configured_hash(),
        )
        app = create_m3_app(
            settings,
            env.world.store,
            env.world.registry,
            providers=lambda _: env.world.provider,
            scopes=(scope,),
        )
        async with app.router.lifespan_context(app):
            rt = app.state.runtime
            assert rt.supervisor.snapshot.state == RunState.STOPPED
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as client:
                login = await client.post(
                    "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
                )
                assert login.status_code == 200
                headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
                current = rt.supervisor.snapshot
                command = dict(
                    type="gm.configure",
                    request_id="http-gm",
                    scope=current.scope.model_dump(mode="json"),
                    target="gm-runtime",
                    expected_version=1,
                    arguments={
                        "schema_id": "m3.gm-configure",
                        "schema_version": "1.0",
                        "data": {"intensity": 0.4},
                    },
                )
                assert (
                    await client.post("/api/v1/commands", json=command, headers={"Origin": ORIGIN})
                ).status_code == 403
                result = await client.post("/api/v1/commands", json=command, headers=headers)
                assert result.status_code == 200, result.text
                assert (await rt.state.gm(current.scope)).intensity == 0.4
                start = dict(
                    type="agent.start",
                    request_id="http-start",
                    scope=current.scope.model_dump(mode="json"),
                    target="primary",
                    expected_version=current.version,
                    arguments={"autonomy": "autonomous"},
                )
                result = await client.post("/api/v1/commands", json=start, headers=headers)
                assert result.status_code == 200, result.text
                original = rt.store.transaction

                def unavailable(*args, **kwargs):
                    raise AssertionError("STOP must not touch storage")

                rt.store.transaction = unavailable
                try:
                    result = await client.post(
                        "/api/v1/agents/primary/stop",
                        headers=headers,
                        json=dict(
                            type="agent.stop",
                            request_id="http-stop",
                            target="primary",
                            scope=rt.supervisor.snapshot.scope.model_dump(mode="json"),
                        ),
                    )
                    assert result.status_code == 200 and result.json()["state"] == "STOPPED"
                finally:
                    rt.store.transaction = original
        assert not env.world.provider.calls

    run(scenario())


@pytest.mark.parametrize("injected_provider", [False, True])
def test_laboratory_job_pins_fresh_test_scope_and_saves_full_mock_report(
    tmp_path, injected_provider
):
    async def scenario():
        env = await setup(tmp_path)
        rt = env.runtime
        scope = env.world.scope.model_copy(
            update={"memory_mode": "test", "memory_space_id": "lab-job"}
        )
        await rt.store.provision(scope, lab_grants() + env.grants)
        rt.scopes += (scope,)

        async def allocate(label):
            return scope

        from ai_agent_application.social import register_social_extensions

        register_social_extensions(rt.registry)
        from ai_agent_application.laboratory.lab import LABORATORY

        reports = Reports(
            tmp_path / "reports",
            rt.store,
            rt.registry,
            clock=env.world.clock,
            readers=(RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),),
        )
        index = SQLiteRecallIndex(tmp_path / "lab-index.sqlite")
        rt.lab = LabRunner(
            rt.store,
            allocate,
            lambda: index,
            clock=VirtualClock(env.world.clock()),
            artifact_writer=reports.save,
        )
        rt.reports = reports
        called = []
        if injected_provider:

            def forbidden_provider(_):
                called.append(True)
                raise AssertionError("M3 mock job reached an injected provider")

            rt.lab.providers = forbidden_provider
        authority = ControlAuthority(
            "operator", frozenset({env.world.scope.partition, scope.partition}), 3600
        )
        job = await rt.submit(
            authority,
            "lab-job",
            TaskInput(
                kind="laboratory",
                trigger_id="lab-request",
                scenario_id="same-nickname",
                lab_scope=scope,
            ),
        )
        completed = await rt.execute(job)
        if injected_provider:
            assert completed.status == OperationState.FAILED
            assert called == [] and rt.lab.scopes == []
            index.close()
            await rt.close()
            return
        assert completed.status == OperationState.SUCCEEDED
        record = (await rt.store.repository(LaboratoryReport, LABORATORY, scope).list())[0]
        assert record.report_artifact is not None and record.status == "passed"
        raw = json.loads(
            await reports.read(
                scope, record.report_artifact.artifact_id, PRINCIPALS[Owner.CATALOGUE]
            )
        )
        assert raw["mock"] and raw["findings"] and raw["log"]
        assert not env.world.provider.calls
        import pytest
        from ai_agent_contracts.codec import ContractError

        with pytest.raises(ContractError):
            await rt.submit(
                authority,
                "reuse-lab",
                TaskInput(
                    kind="laboratory",
                    trigger_id="lab-request",
                    scenario_id="same-nickname",
                    lab_scope=scope,
                ),
            )
        index.close()
        await rt.close()

    run(scenario())
