"""GLM-OC-B: two fictional personas through the OpenCode bridge surface.

Additional independent acceptance on the production build/SQLite/memory/session owners
with a scripted synthetic provider (no network, no paid calls). What is asserted here is
the native-UI surface itself, on top of the owner-level suites in tests/workplace_sessions:

- A and B keep distinct profiles, histories and memory partitions; browsing B's sessions
  through the bridge never switches the single active runtime executor;
- the synthetic model request body for A's turn carries A's canon/history markers and never
  B's; swapped command scopes, foreign session ids and cross-directory reads are refused;
- repeated transcript reads and a fresh browser login are inert: no START, no duplicate
  job/command/ACK, no second model call;
- a basis revoked while the model call is in flight closes the late result at the bridge
  projection; direct STOP and a full host restart keep STOPPED and never retry.
"""

import asyncio

from ai_agent_application.agent_workplace.ports import HarnessEvent, ModelRequest
from ai_agent_application.llm import ChatMessage
from ai_agent_application.storage import Owner
from ai_agent_contracts.control import Utterance
from ai_agent_contracts.persona import CharacterState, Profile
from session_support import install, synthetic
from test_activity import ACTIVITY_MARKER, WorkModel, fast, prepared, until

from ai_agent_core import bootstrap
from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import work_sessions
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, put
from ai_agent_core.m3_composition.state import command as owner_command
from ai_agent_core.opencode_ui.protocol import PREFIX, directory

ALPHA_CANON = "Канон вымышленной личности А. Тайный знак канона: АЛЬФА-4К91."
BETA_CANON = "Канон вымышленной личности Б. Тайный знак канона: БЕТА-8М77."
ALPHA_NOTE = "Примета истории А: ЗНАК-А-5В."
BETA_NOTE = "Примета истории Б: ЗНАК-Б-3Н."


def base(scope):
    return PREFIX + "/agents/" + directory(scope)


def opened(scope, key):
    return dict(
        type="work.session.open",
        request_id=key,
        target="work",
        scope=scope.model_dump(mode="json"),
        arguments=dict(schema_id="work.session.open-args", data={"title": "GLM приёмка"}),
    )


def message(scope, identifier, *, text, key, version=1):
    return dict(
        type="work.session.message",
        request_id=key,
        target=identifier,
        scope=scope.model_dump(mode="json"),
        expected_version=version,
        arguments=dict(
            schema_id="work.session.message-args", data={"session_id": identifier, "text": text}
        ),
    )


def acknowledge(draft, key):
    return dict(
        type="dialogue.acknowledge",
        request_id=key,
        target=draft["utterance_id"],
        scope=draft["scope"],
        expected_version=draft["version"],
        arguments={"revision": draft["revision"]},
    )


async def command(host, payload, *, selected=None, headers=None):
    return await host.env.client.post(
        base(selected or host.scope) + "/commands",
        json=payload,
        headers=host.env.headers if headers is None else headers,
    )


async def view(host, identifier, scope=None):
    result = await host.env.client.get(base(scope or host.scope) + "/sessions/" + identifier)
    assert result.status_code == 200, result.text
    return result.json()


async def seed_second_persona(host, surfaces, canon):
    """Fixture setup through the owners: a prepared inactive partition for persona B."""
    rt = host.runtime
    inactive = host.scope.model_copy(
        update={"persona_id": "glm-beta", "memory_space_id": "glm-beta-private"}
    )
    await rt.store.provision(inactive, rights.host_grants(load_config(paths(host.root))))
    profile = await rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], host.scope).get(
        host.scope.persona_id
    )
    second = profile.model_copy(
        update={
            "id": inactive.persona_id,
            "persona_id": inactive.persona_id,
            "scope": inactive,
            "canon": canon,
            "version": 1,
        }
    )
    await put(
        rt.store, rt.registry, Owner.PERSONA, inactive, "seed-glm-beta", second, None, rt.readers
    )
    rt.scopes = (*rt.scopes, inactive)
    surfaces[-1].sessions.scopes = (*surfaces[-1].sessions.scopes, inactive)
    return inactive


def capture_surfaces(monkeypatch):
    surfaces = []
    original = bootstrap.install_access

    def installed(*args, **kwargs):
        surface = original(*args, **kwargs)
        surfaces.append(surface)
        return surface

    monkeypatch.setattr(bootstrap, "install_access", installed)
    return surfaces


async def seed_canon(host, canon):
    """Fixture setup: give the active persona A a distinct canon marker through the owners.

    CharacterState pins profile_version; the readiness context refuses a desynchronised
    pair, so both records move together exactly like a real profile acceptance would.
    """
    rt = host.runtime
    profiles = rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], host.scope)
    states = rt.store.repository(CharacterState, PRINCIPALS[Owner.PERSONA], host.scope)
    profile = await profiles.get(host.scope.persona_id)
    updated = profile.model_copy(
        update={
            "canon": canon,
            "version": profile.version + 1,
            "updated_at": rt.supervisor.clock.utc(),
        }
    )
    await put(
        rt.store,
        rt.registry,
        Owner.PERSONA,
        host.scope,
        "seed-glm-alpha-canon",
        updated,
        profile.version,
        rt.readers,
    )
    current = (await states.list(limit=2))[0]
    state = current.model_copy(
        update={
            "profile_version": updated.version,
            "version": current.version + 1,
            "state_version": current.state_version + 1,
            "updated_at": rt.supervisor.clock.utc(),
        }
    )
    await put(
        rt.store,
        rt.registry,
        Owner.PERSONA,
        host.scope,
        "seed-glm-alpha-state",
        state,
        current.version,
        rt.readers,
    )


async def restart_login(host):
    login = await host.env.client.post(
        "/api/v1/auth/login",
        headers={"Origin": host.env.headers["Origin"]},
        json={"password": paths(host.root).password.read_text().strip()},
    )
    assert login.status_code == 200
    host.env.headers["X-CSRF-Token"] = login.json()["csrf_token"]


def test_two_personas_distinct_histories_and_isolated_model_request(tmp_path, monkeypatch):
    async def scenario():
        surfaces = capture_surfaces(monkeypatch)
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope)
            await seed_canon(host, ALPHA_CANON)

            # Persona A stores one work-session message while STOPPED.
            alpha = (await command(host, opened(scope, "glm-open-a"))).json()["resource_id"]
            stored = await command(host, message(scope, alpha, text=ALPHA_NOTE, key="glm-say-a"))
            assert stored.status_code == 200, stored.text
            assert model.count() == 0 and rt.supervisor.snapshot.state.value == "STOPPED"

            inactive = await seed_second_persona(host, surfaces, BETA_CANON)
            await restart_login(host)
            catalogue = (await host.env.client.get(PREFIX + "/agents")).json()["items"]
            assert {c["profile_id"] for c in catalogue} >= {scope.persona_id, "glm-beta"}
            assert [c["directory"] for c in catalogue if c["active_runtime"]] == [
                directory(scope)
            ]

            # Persona B is viewable and accepts durable intake, but never becomes the executor.
            beta = (
                await command(host, opened(inactive, "glm-open-b"), selected=inactive)
            ).json()["resource_id"]
            kept = await command(
                host, message(inactive, beta, text=BETA_NOTE, key="glm-say-b"), selected=inactive
            )
            assert kept.status_code == 200, kept.text
            assert rt.supervisor.snapshot.scope.partition == scope.partition
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count() == 0

            # Histories are distinct and do not leak across the bridge projections.
            alpha_view, beta_view = await view(host, alpha), await view(host, beta, inactive)
            assert [e["text"] for e in alpha_view["entries"]] == [ALPHA_NOTE]
            assert [e["text"] for e in beta_view["entries"]] == [BETA_NOTE]
            listed = (
                await host.env.client.get(base(scope) + "/sessions")
            ).json()["items"]
            assert [s["id"] for s in listed] == [alpha]
            foreign = await host.env.client.get(base(scope) + "/sessions/" + beta)
            assert foreign.status_code == 403  # a foreign partition id is denied, not leaked

            # Browsing B repeatedly never switches the active executor or calls the model.
            for _ in range(3):
                await view(host, beta, inactive)
                await view(host, alpha)
            assert rt.supervisor.snapshot.scope.partition == scope.partition
            assert model.count() == 0

            # Identity substitution through the command surface is refused.
            swapped = await command(host, message(inactive, beta, text="Чужой текст", key="x1"))
            assert swapped.status_code == 403
            wrong_target = await command(
                host, message(scope, beta, text="Подмена цели", key="x2")
            )
            assert wrong_target.status_code != 200
            assert (await view(host, beta, inactive))["entries"][-1]["text"] == BETA_NOTE

            # A's turn: the scripted request body itself carries A's markers, never B's.
            model.queues["activity"].append({"reply": "Синтетический ответ личности А"})
            run = rt.supervisor.snapshot
            start = dict(
                type="agent.start",
                request_id="glm-start-a",
                target="primary",
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={"autonomy": "autonomous"},
            )
            assert (await command(host, start)).status_code == 200
            pending = await until(
                lambda: view(host, alpha),
                lambda v: v.get("pending_reply") and v["pending_reply"]["acknowledgeable"],
                "alpha draft",
                seconds=60,
            )
            body = model.last("activity")
            assert ACTIVITY_MARKER in body["messages"][0]["content"]
            prompt = body["messages"][-1]["content"]
            assert "АЛЬФА-4К91" in prompt and "ЗНАК-А-5В" in prompt
            assert "БЕТА-8М77" not in prompt and "ЗНАК-Б-3Н" not in prompt

            # One exact ACK; its replay is the same receipt, not a second acknowledgement.
            draft = pending["pending_reply"]
            ack = await command(host, acknowledge(draft, "glm-ack-a"))
            assert ack.status_code == 200, ack.text
            assert (await command(host, acknowledge(draft, "glm-ack-a"))).json() == (
                ack.json()
            )
            final = await view(host, alpha)
            assert final["pending_reply"] is None and final["processed_sequence"] == 2
            assert model.count("activity") == 1
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                "/api/v1/agents/" + run.agent_id + "/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="glm-stop-a",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200
            assert rt.supervisor.snapshot.state.value == "STOPPED"

    asyncio.run(scenario())


def test_reload_restart_stop_and_revoked_late_result_through_bridge(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope)
            called, release = asyncio.Event(), asyncio.Event()

            class Held:
                def __init__(self, gateway):
                    self.gateway = gateway

                async def turn(self, gate, turn):
                    reply = await self.gateway.generate(
                        gate,
                        ModelRequest(
                            "activity",
                            (
                                ChatMessage("system", ACTIVITY_MARKER),
                                ChatMessage("user", turn.instruction),
                            ),
                            max_output_tokens=64,
                            call_id="glm-held",
                        ),
                    )
                    called.set()
                    await release.wait()  # the operator revokes the basis meanwhile
                    return [HarnessEvent("result", reply.text)]

                async def close(self, key):
                    pass

                async def cancel(self, key):
                    pass

            async def factory(execution, gateway, gate):
                return Held(gateway)

            rt.session_harness = factory
            sid = (await command(host, opened(scope, "glm-open-late"))).json()["resource_id"]
            first = await command(host, message(scope, sid, text=ALPHA_NOTE, key="glm-say-late"))
            assert first.status_code == 200, first.text
            model.queues["activity"].append({"reply": "Поздний синтетический ответ"})
            run = rt.supervisor.snapshot
            start = dict(
                type="agent.start",
                request_id="glm-start-late",
                target="primary",
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={"autonomy": "autonomous"},
            )
            assert (await command(host, start)).status_code == 200
            await asyncio.wait_for(called.wait(), 60)

            # Reload and a fresh browser login are inert while the turn is in flight.
            before = await view(host, sid)
            await restart_login(host)
            for _ in range(3):
                repeated = await view(host, sid)
                assert repeated["entries"] == before["entries"]
            assert model.count("activity") == 1
            assert rt.supervisor.snapshot.scope.partition == scope.partition

            # The operator message basis is revoked mid-call: the late result is closed.
            rows = await work_sessions.sessions(rt).messages(scope, sid, limit=200)
            await memory(rt).set_rights(
                owner_command(scope, "glm-revoke"),
                rows[0].source.source_id,
                rows[0].source.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            release.set()
            closed = await until(
                lambda: view(host, sid),
                lambda v: v["state"] == "invalidated",
                "late result closed",
                seconds=60,
            )
            assert closed["invalidated_reason"] == "message_unavailable"
            assert closed["pending_reply"] is None and closed["entries"] == []
            drafts = await rt.store.repository(Utterance, PRINCIPALS[Owner.GATEWAY], scope).list(
                limit=200
            )
            assert not [u for u in drafts if "Поздний синтетический ответ" in u.content]
            await asyncio.sleep(1.0)
            assert model.count("activity") == 1  # no blind retry of the closed basis

            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                "/api/v1/agents/" + run.agent_id + "/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="glm-stop-late",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200
            assert rt.supervisor.snapshot.state.value == "STOPPED"

        # Full host restart: STOPPED stays, views survive, nothing is replayed or retried.
        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            after = await view(host, sid)
            assert after["state"] == "invalidated" and after["entries"] == []
            assert model.count("activity") == 1
            # Same request id after restart: the durable receipt replays, nothing re-runs.
            replay = await command(host, message(scope, sid, text=ALPHA_NOTE, key="glm-say-late"))
            assert replay.status_code == 200, replay.text
            assert replay.json() == first.json()
            assert model.count("activity") == 1
            assert (await view(host, sid))["entries"] == []

            # A new conversation is the honest continuation path after restart.
            fast(rt)
            synthetic(rt)
            fresh = (await command(host, opened(scope, "glm-open-fresh"))).json()["resource_id"]
            model.queues["activity"].append({"reply": "Свежий синтетический ответ"})
            fresh_message = message(scope, fresh, text="Новое сообщение", key="glm-say-fresh")
            assert (await command(host, fresh_message)).status_code == 200
            run = rt.supervisor.snapshot
            start = dict(
                type="agent.start",
                request_id="glm-start-fresh",
                target="primary",
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={"autonomy": "autonomous"},
            )
            assert (await command(host, start)).status_code == 200
            answered = await until(
                lambda: view(host, fresh),
                lambda v: v.get("pending_reply") and v["pending_reply"]["acknowledgeable"],
                "fresh draft",
                seconds=60,
            )
            assert "Свежий синтетический ответ" in answered["pending_reply"]["text"]
            assert model.count("activity") == 2

    asyncio.run(scenario())
