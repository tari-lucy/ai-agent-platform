"""Production host memory admission and role wiring with synthetic HTTP only."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from ai_agent_adapters.llm import OpenAICompatibleProvider
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_application.storage import Owner
from ai_agent_contracts.llm import LLMAssignment
from test_http import finish_job, post, scope_params, start
from test_live import answer, binding

from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.init import initialise, work_scope
from ai_agent_core.local_host.layout import HostError, LiveConfig, load_config, paths
from ai_agent_core.local_host.memory import enable_memory
from ai_agent_core.local_host.process import exclusive_host
from ai_agent_core.local_host.serve import build
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import MEMORY_ROLES, PRINCIPALS


def test_enable_memory_roles_and_real_adapter_pipeline_are_explicit_and_bounded(
    tmp_path, monkeypatch
):
    async def scenario():
        root = tmp_path / "host"
        await initialise(
            root,
            profile_markdown=REFERENCE_PROFILE,
            operator_controls=True,
            live=LiveConfig(model_id="fixture-model", attempt_limit=300, attempt_period="utc_day"),
        )
        with exclusive_host(root):
            with pytest.raises(HostError, match="HOST_ALREADY_RUNNING"):
                await enable_memory(root)
        await enable_memory(root)
        config = load_config(paths(root))
        assert config.live.memory_processing and config.live.attempt_limit == 300
        before = paths(root).database.read_bytes()
        await enable_memory(root)
        assert paths(root).database.read_bytes() == before
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        store = SQLiteStore(paths(root).database, extensions=new_registry())
        try:
            roles = await store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope).list(
                limit=200
            )
            assert {r.role_id for r in roles if r.connection_id == "conn-live"} == {
                "character",
                *MEMORY_ROLES,
            }
        finally:
            await store.close()
        payloads = [dict(reply="Вымышленный ответ о зонтике.")]
        calls = []

        async def send(request):
            body = json.loads(request.content)
            calls.append(body)
            assert body["model"] == "fixture-model" and "tools" not in body
            assert len(calls) <= 4
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=answer(json.dumps(payloads.pop(0), ensure_ascii=False)),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as wire:
            monkeypatch.setattr(
                providers,
                "OpenAICompatibleProvider",
                lambda settings: OpenAICompatibleProvider(settings, wire),
            )
            app, close = build(
                paths(root), config, provider="live", env_file=binding(tmp_path / "synthetic.env")
            )
            try:
                async with app.router.lifespan_context(app):
                    rt = app.state.runtime
                    assert rt.supervisor.snapshot.state.value == "STOPPED" and not calls
                    origin = "http://127.0.0.1:8765"
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=origin
                    ) as client:
                        login = await client.post(
                            "/api/v1/auth/login",
                            json={"password": paths(root).password.read_text().strip()},
                            headers={"Origin": origin},
                        )
                        assert login.status_code == 200
                        env = SimpleNamespace(
                            runtime=rt,
                            client=client,
                            headers={"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]},
                        )
                        run = await start(env)
                        made = await post(
                            env,
                            dict(
                                type="dialogue.message",
                                request_id="input",
                                target=run.session_id,
                                scope=run.scope.model_dump(mode="json"),
                                arguments={"text": "Вымышленная Ника любит синие зонтики."},
                            ),
                        )
                        assert made.status_code == 200, made.text
                        await finish_job(env, made.json()["operation_id"])
                        query = scope_params(scope)
                        reply = (await client.get("/api/v1/utterances", params=query)).json()[
                            "items"
                        ][0]
                        ack = await post(
                            env,
                            dict(
                                type="dialogue.acknowledge",
                                request_id="received",
                                target=reply["id"],
                                scope=run.scope.model_dump(mode="json"),
                                expected_version=reply["version"],
                                arguments={"revision": reply["revision"]},
                            ),
                        )
                        assert ack.status_code == 200, ack.text
                        episode = (await client.get("/api/v1/episodes", params=query)).json()[
                            "items"
                        ][0]
                        payloads.extend(
                            [
                                dict(proposals=[]),
                                dict(content="Вымышленный разговор о зонтиках."),
                                dict(
                                    keep_state_reason="Нет оснований менять характер.", changes=[]
                                ),
                            ]
                        )
                        cycle = await post(
                            env,
                            dict(
                                type="session.process-memory",
                                request_id="process",
                                target=run.session_id,
                                scope=run.scope.model_dump(mode="json"),
                                expected_version=rt.supervisor.snapshot.version,
                                arguments={"episode_ids": [episode["id"]]},
                            ),
                        )
                        assert cycle.status_code == 200, cycle.text
                        await finish_job(env, cycle.json()["operation_id"])
                        rows = (await client.get("/api/v1/memory-cycles", params=query)).json()[
                            "items"
                        ]
                        assert len(rows) == 1 and rows[0]["status"] == "completed"
                        status = (
                            await client.get("/api/v1/private-api-status", params=query)
                        ).json()
                        assert status["attempts_used"] == 4 and status["memory_calls_used"] == 3
                        assert (
                            status["dialogue_remaining"] == 291 and status["attempt_limit"] == 300
                        )
                        assert len(calls) == 4 and not payloads
                        assert "Вымышленная Ника" in calls[-1]["messages"][-1]["content"]
            finally:
                close()

    asyncio.run(scenario())


def test_pending_enable_refuses_serve_and_memory_needs_daily_controls(tmp_path):
    async def scenario():
        root = tmp_path / "host"
        await initialise(
            root, profile_markdown=REFERENCE_PROFILE, live=LiveConfig(model_id="fixture-model")
        )
        before = paths(root).database.read_bytes()
        with pytest.raises(HostError, match="MEMORY_CONTROLS_REQUIRED"):
            await enable_memory(root)
        assert paths(root).database.read_bytes() == before
        (root / "MEMORY-ENABLE-PENDING").write_text("synthetic interrupted admission")
        with pytest.raises(HostError, match="MEMORY_ENABLE_INCOMPLETE"):
            load_config(paths(root))

    asyncio.run(scenario())
