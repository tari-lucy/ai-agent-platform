"""Production host work-session scheduling and ACK, synthetic harness/provider only."""

import asyncio
from datetime import timedelta

from ai_agent_application.agent_workplace.ports import HarnessEvent, ModelRequest
from ai_agent_application.llm import ChatMessage
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import CapabilityBinding, CapabilityManifest, WorkAttempt
from ai_agent_contracts.api import Permission
from ai_agent_contracts.memory import Episode, Source
from test_activity import ACTIVITY_MARKER, WorkModel, fast, prepared, until
from test_http import post
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.m3_composition.state import PRINCIPALS, extension, put, record

CAPABILITY = "workplace.synthetic-session"


async def install(rt, scope):
    now = rt.supervisor.clock.utc()
    common = dict(scope=scope, version=1, created_at=now, updated_at=now)
    manifest = CapabilityManifest(
        id="session-manifest",
        **common,
        capability_id=CAPABILITY,
        capability_version=1,
        summary="Synthetic session fixture",
        instructions="Synthetic only",
        input_schema_id="session.instruction",
        output_schema_id="session.result",
        effect="local_write",
        areas=["inbox"],
        network="gateway",
        runner_required=True,
    )
    binding = CapabilityBinding(
        id="session-binding",
        binding_id="session-binding",
        **common,
        capability_id=CAPABILITY,
        capability_version=1,
        adapter_id="synthetic",
        adapter_version="1",
        state="ready",
        verified_at=now,
    )
    permission = record(
        rt.registry,
        Permission,
        id="session-permission",
        **common,
        principal_id="workplace-agent",
        capability_id=CAPABILITY,
        operation_type=CAPABILITY,
        binding_id=binding.id,
        level="automatic",
        conditions=extension(rt.registry, "m3.empty", {}),
        limits=extension(rt.registry, "m3.empty", {}),
        policy_version=1,
        author_id="operator",
        expires_at=now + timedelta(days=1),
    )
    for item in (manifest, binding, permission):
        await put(
            rt.store, rt.registry, Owner.WORKSPACE, scope, "seed-" + item.id, item, None, rt.readers
        )

    class Harness:
        def __init__(self, gateway):
            self.gateway = gateway

        async def turn(self, gate, turn):
            reply = await self.gateway.generate(
                gate,
                ModelRequest(
                    "activity",
                    (ChatMessage("system", ACTIVITY_MARKER), ChatMessage("user", turn.instruction)),
                    max_output_tokens=64,
                    call_id="session-answer",
                ),
            )
            return [HarnessEvent("result", reply.text)] if reply.status == "completed" else []

        async def close(self, key):
            pass

    async def factory(execution, gateway, gate):
        return Harness(gateway)

    rt.session_harness = factory
    rt.session_capability = CAPABILITY


def ack(host, draft, key="work-ack"):
    return post(
        host.env,
        dict(
            type="dialogue.acknowledge",
            request_id=key,
            scope=draft["scope"],
            target=draft["utterance_id"],
            expected_version=draft["version"],
            arguments={"revision": draft["revision"]},
        ),
    )


def test_session_response_waits_for_exact_ack_and_uses_existing_owners(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            fast(rt)
            failures = []
            original_perform = rt._perform

            async def observed(execution):
                try:
                    return await original_perform(execution)
                except Exception as error:
                    failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", observed)
            from ai_agent_core.m3_composition import work_session_delivery

            original_ack = work_session_delivery.acknowledge

            async def observed_ack(*args):
                try:
                    return await original_ack(*args)
                except Exception as error:
                    failures.append(error)
                    raise

            monkeypatch.setattr(work_session_delivery, "acknowledge", observed_ack)

            async def checked_view(identifier):
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            await install(rt, host.scope)
            opened = await open_session(host, "session-runtime")
            identifier = opened.json()["resource_id"]
            assert (
                await say(host, "question", identifier, 1, "Synthetic lighthouse question")
            ).status_code == 200
            await asyncio.sleep(0.1)
            assert model.count("activity") == 0
            model.queues["activity"].append({"reply": "Synthetic response"})
            await host.start()
            view = await until(
                lambda: checked_view(identifier),
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "queued session response",
            )
            assert len(view["entries"]) == 1 and view["processed_sequence"] == 0
            assert "Synthetic response" in view["pending_reply"]["text"]
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            assert not any(s.source_type == "work-session-reply" for s in sources)
            response = await ack(host, view["pending_reply"])
            if failures:
                raise failures[0]
            assert response.status_code == 200, response.text
            again = await ack(host, view["pending_reply"])
            assert again.status_code == 200, again.text
            view = await transcript(host, identifier)
            assert view["pending_reply"] is None and len(view["entries"]) == 2
            assert view["entries"][-1]["speaker"] == "character" and view["processed_sequence"] == 2
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            assert sum(s.source_type == "work-session-reply" for s in sources) == 1
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list(limit=200)
            assert (
                sum(
                    e.actual_outcome == "Панель подтвердила получение точной редакции."
                    for e in episodes
                )
                == 1
            )
            attempts = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).list(limit=200)
            assert (
                len(attempts) == 1
                and attempts[0].session_id == identifier
                and attempts[0].task_id is None
            )
            assert model.count("activity") == 1

    asyncio.run(scenario())
