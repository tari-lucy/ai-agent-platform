"""Actual authenticated owner composition, SQLite and private stream; provider is scripted."""

import asyncio
import json

import pytest
from ai_agent_adapters.llm.mock import Pause
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import EventSubscription, Permission
from ai_agent_contracts.control import Utterance
from ai_agent_contracts.llm import LLMAttempt
from ai_agent_contracts.memory import Episode
from ai_agent_contracts.persona import Profile, ReflectionCheckpoint
from ai_agent_contracts.states import OperationState
from dev_support import answer
from integration_support import prepared

from ai_agent_core.m3_composition.state import PRINCIPALS, put


def scope_params(scope):
    return dict(
        zip(
            ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
            scope.partition,
            strict=True,
        )
    )


async def post(env, value):
    return await env.client.post("/api/v1/commands", json=value, headers=env.headers)


async def start(env):
    run = env.runtime.supervisor.snapshot
    response = await post(
        env,
        dict(
            type="agent.start",
            request_id="start-http",
            target="primary",
            scope=run.scope.model_dump(mode="json"),
            expected_version=run.version,
            arguments={"autonomy": "autonomous"},
        ),
    )
    assert response.status_code == 200, response.text
    return env.runtime.supervisor.snapshot


async def finish_job(env, operation_id, *, succeeded=True):
    # Observe the actual lifespan executor, never race it with a second manual claim.
    for _ in range(200):
        stored = await env.runtime.jobs.store.load(
            operation_id, env.runtime.supervisor.snapshot.scope
        )
        assert stored is not None
        if stored[0].status in (
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.OUTCOME_UNKNOWN,
        ):
            if succeeded:
                assert stored[0].status == OperationState.SUCCEEDED, stored[0].status
            return stored[0]
        await asyncio.sleep(0.05)
    raise AssertionError("LOCAL_JOB_DID_NOT_FINISH")


def test_actual_login_message_private_receipt_reflection_and_restart(tmp_path):
    async def scenario():
        async with prepared(
            tmp_path,
            [
                answer(reply="Вымышленный приватный ответ."),
                answer(keep_state_reason="Одного эпизода мало.", changes=[]),
            ],
        ) as env:
            rt, scope = env.runtime, env.world.scope
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            run = await start(env)
            client_stream = rt.events.subscribe(EventSubscription(scope=run.scope, resource_ids=[]))
            body = dict(
                type="dialogue.message",
                request_id="private-msg",
                target=run.session_id,
                scope=run.scope.model_dump(mode="json"),
                arguments={"text": "Вымышленное приветствие."},
            )
            accepted = await post(env, body)
            assert accepted.status_code == 200, accepted.text
            operation = accepted.json()["operation_id"]
            from ai_agent_contracts.control import ExecutionCommand

            known = await rt.jobs.store.load(operation, run.scope)
            execution = await rt.store.repository(
                ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], run.scope
            ).get(known[0].command_id)
            assert "Вымышленное приветствие." not in execution.model_dump_json()
            assert len(execution.sources) == 1

            assert (await post(env, body)).json()["operation_id"] == operation
            conflict = await post(env, {**body, "arguments": {"text": "Другое содержание."}})
            assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
            await finish_job(env, operation)
            query = scope_params(scope)
            utterances = await env.client.get("/api/v1/utterances", params=query)
            assert utterances.status_code == 200, utterances.text
            reply = utterances.json()["items"][0]
            assert reply["content"] == "Вымышленный приватный ответ."
            assert reply["moderation_status"] == "draft"  # no fake public approval
            episodes = await env.client.get("/api/v1/episodes", params=query)
            assert episodes.json()["items"] == []  # saved draft is not a delivered experience
            events = [
                json.loads(raw)
                for raw in client_stream.buffer
                if json.loads(raw)["type"].startswith("generation.")
            ]
            assert any(e["type"] == "generation.chunk" and e["preliminary"] for e in events)
            assert (
                events[-1]["type"] == "generation.terminal"
                and events[-1]["saved_result_id"] == reply["id"]
            )
            attempts = await env.client.get("/api/v1/llm-attempts", params=query)
            assert len(attempts.json()["items"]) == 1
            assert attempts.json()["items"][0]["scope"]["session_id"] == run.session_id
            assert attempts.json()["items"][0]["operation_id"].startswith(operation + ".")
            ack = dict(
                type="dialogue.acknowledge",
                request_id="private-receipt",
                target=reply["id"],
                scope=run.scope.model_dump(mode="json"),
                expected_version=reply["version"],
                arguments={"revision": reply["revision"]},
            )
            result = await post(env, ack)
            assert result.status_code == 200, result.text
            assert (await post(env, ack)).json() == result.json()
            episodes = await env.client.get("/api/v1/episodes", params=query)
            assert len(episodes.json()["items"]) == 1
            episode_id = episodes.json()["items"][0]["id"]
            assert len((await env.client.get("/api/v1/sources", params=query)).json()["items"]) == 2
            reflect = dict(
                type="session.reflect",
                request_id="closing-reflection",
                target=run.session_id,
                scope=run.scope.model_dump(mode="json"),
                expected_version=rt.supervisor.snapshot.version,
                arguments={"episode_ids": [episode_id]},
            )
            result = await post(env, reflect)
            assert result.status_code == 200, result.text
            await finish_job(env, result.json()["operation_id"])
            checkpoints = await rt.store.repository(
                ReflectionCheckpoint, PRINCIPALS[Owner.PERSONA], scope
            ).list()
            assert len(checkpoints) == 1 and checkpoints[0].completed
            operations = await env.client.get("/api/v1/operations", params=query)
            assert len(operations.json()["items"]) == 2
            assert all(o["status"] == "SUCCEEDED" for o in operations.json()["items"])
            result = await post(
                env,
                dict(
                    type="session.end",
                    request_id="end-http",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                    expected_version=rt.supervisor.snapshot.version,
                    arguments={},
                ),
            )
            assert result.status_code == 200, result.text
            # Direct STOP stays independent of storage/model.
            stopped = await env.client.post(
                "/api/v1/agents/primary/stop",
                headers=env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="stop-http",
                    target="primary",
                    scope=rt.supervisor.snapshot.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200 and stopped.json()["state"] == "STOPPED"
            replay = await post(env, body)
            assert replay.status_code == 200 and replay.json()["operation_id"] == operation
            assert (await post(env, {**body, "request_id": "new-after-stop"})).status_code == 409
            assert len(env.world.provider.calls) == 2
            rt.events.remove(client_stream)
            await rt.close()
            await rt.open()
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert len(env.world.provider.calls) == 2
            assert (
                len(await rt.store.repository(Episode, PRINCIPALS[Owner.MEMORY], scope).list()) == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("close", ["stop", "pause", "permission", "source_rights"])
def test_stop_or_permission_revocation_during_model_cannot_save_reply(tmp_path, close):
    async def scenario():
        script = answer(reply="Поздний ответ.")
        script.insert(1, Pause(0.3))
        async with prepared(tmp_path, [script]) as env:
            run = await start(env)
            accepted = await post(
                env,
                dict(
                    type="dialogue.message",
                    request_id="late-msg",
                    target=run.session_id,
                    scope=run.scope.model_dump(mode="json"),
                    arguments={"text": "Вымышленный вопрос."},
                ),
            )
            assert accepted.status_code == 200, accepted.text
            for _ in range(800):
                if env.world.provider.calls:
                    break
                await asyncio.sleep(0.01)
            assert env.world.provider.calls
            if close == "stop":
                response = await env.client.post(
                    "/api/v1/agents/primary/stop",
                    headers=env.headers,
                    json=dict(
                        type="agent.stop",
                        request_id="stop-mid",
                        target="primary",
                        scope=run.scope.model_dump(mode="json"),
                    ),
                )
                assert response.status_code == 200
            elif close == "permission":
                old = await env.runtime.store.repository(
                    Permission, PRINCIPALS[Owner.WORKSPACE], run.scope
                ).get("permit-dialogue")
                await put(
                    env.runtime.store,
                    env.world.registry,
                    Owner.WORKSPACE,
                    run.scope,
                    "revoke",
                    old.model_copy(
                        update=dict(
                            version=old.version + 1,
                            scope=run.scope,
                            revoked_at=env.runtime.supervisor.clock.utc(),
                            updated_at=env.runtime.supervisor.clock.utc(),
                        )
                    ),
                    old.version,
                )
            elif close == "pause":
                response = await post(
                    env,
                    dict(
                        type="agent.pause",
                        request_id="pause-mid",
                        target="primary",
                        scope=run.scope.model_dump(mode="json"),
                        expected_version=env.runtime.supervisor.snapshot.version,
                        arguments={},
                    ),
                )
                assert response.status_code == 200
            else:
                from ai_agent_contracts.memory import Source

                from ai_agent_core.m3_composition.dialogue import memory
                from ai_agent_core.m3_composition.state import command

                sources = await env.runtime.store.repository(
                    Source, PRINCIPALS[Owner.MEMORY], run.scope
                ).list()
                incoming = sources[0]
                await memory(env.runtime).set_rights(
                    command(run.scope, "revoke-source"),
                    incoming.id,
                    incoming.version,
                    read_allowed=True,
                    api_transfer_allowed=False,
                    public_disclosure_allowed=False,
                )
            await finish_job(env, accepted.json()["operation_id"], succeeded=False)
            assert (
                await env.runtime.store.repository(
                    Utterance, PRINCIPALS[Owner.GATEWAY], run.scope
                ).list()
                == []
            )
            assert (
                await env.runtime.store.repository(
                    Episode, PRINCIPALS[Owner.MEMORY], run.scope
                ).list()
                == []
            )
            attempts = await env.runtime.store.repository(
                LLMAttempt, PRINCIPALS[Owner.LLM], run.scope
            ).list()
            assert len(attempts) == 1 and attempts[0].status != "running"

    asyncio.run(scenario())


def test_canonical_profile_gm_catalogue_and_fail_closed_inputs(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            scope, rt = env.world.scope, env.runtime
            query = scope_params(scope)
            markdown = await env.client.get(
                "/api/v1/persona-markdown/" + scope.persona_id, params=query
            )
            assert markdown.status_code == 200, markdown.text
            profile = await rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], scope).get(
                scope.persona_id
            )
            draft = markdown.json()["markdown"].replace(
                profile.canon, profile.canon + " Уточнённый канон.", 1
            )
            preview = dict(
                type="persona.validate",
                request_id="preview",
                scope=scope.model_dump(mode="json"),
                target=scope.persona_id,
                expected_version=markdown.json()["version"],
                arguments={"schema_id": "persona.validate-args", "data": {"markdown": draft}},
            )
            response = await env.client.post(
                "/api/v1/personas/" + scope.persona_id + "/validate",
                headers=env.headers,
                json=preview,
            )
            assert response.status_code == 200, response.text
            assert (
                response.json()["digest"]
                and response.json()["current_version"] == markdown.json()["version"]
            )
            apply = {
                **preview,
                "type": "persona.apply",
                "request_id": "apply-profile",
                "arguments": {
                    "schema_id": "persona.apply-args",
                    "data": {"markdown": draft, "revision_digest": response.json()["digest"]},
                },
            }
            changed = await post(env, apply)
            assert changed.status_code == 200, changed.text
            assert changed.json()["version"] == markdown.json()["version"] + 1
            stale_profile = await post(env, {**apply, "request_id": "stale-profile"})
            assert stale_profile.status_code == 409
            settings = await env.client.get("/api/v1/gm-settings", params=query)
            assert settings.status_code == 200 and len(settings.json()["items"]) == 1
            gm = settings.json()["items"][0]
            command = dict(
                type="gm.configure",
                request_id="gm-canonical",
                scope=scope.model_dump(mode="json"),
                target=gm["id"],
                expected_version=gm["version"],
                arguments={"schema_id": "gm.configure-args", "data": {"intensity": 0.4}},
            )
            wrong = await post(env, {**command, "target": "arbitrary-session"})
            assert wrong.status_code == 400
            assert (await post(env, command)).status_code == 200
            stale = await post(env, {**command, "request_id": "gm-stale"})
            assert stale.status_code == 409
            catalogue = await env.client.get("/api/v1/laboratory-catalogue", params=query)
            assert catalogue.status_code == 200
            assert (
                any(
                    s["scenario_id"] == "same-nickname" and s["version"] == 1
                    for s in catalogue.json()["scenarios"]
                )
                and catalogue.json()["mode"] == "mock"
            )
            run = await start(env)
            lab = dict(
                type="laboratory.start",
                request_id="lab-invalid",
                target="laboratory",
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={
                    "schema_id": "laboratory.start-args",
                    "data": {
                        "scenario_id": "same-nickname",
                        "scenario_version": 999,
                        "seed": "fixed",
                        "lab_scope": {
                            **scope.model_dump(mode="json"),
                            "memory_mode": "test",
                            "memory_space_id": "unprepared",
                        },
                        "mode": "mock",
                    },
                },
            )
            assert (await post(env, lab)).status_code == 409
            lab["arguments"]["data"]["mode"] = "real"
            assert (await post(env, lab)).status_code == 400
            foreign = await env.client.get(
                "/api/v1/laboratory-catalogue", params={**query, "persona_id": "other"}
            )
            assert foreign.status_code == 403
            assert not env.world.provider.calls

    asyncio.run(scenario())
