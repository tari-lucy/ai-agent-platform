"""OC-01 over production local-host HTTP/owners; synthetic transport, no paid calls."""

import asyncio

from ai_agent_application.storage import Owner
from ai_agent_contracts.common import Scope
from ai_agent_contracts.persona import Profile
from session_support import install
from test_activity import WorkModel, fast, prepared, until

from ai_agent_core import bootstrap
from ai_agent_core.local_host import rights
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition.state import PRINCIPALS, put
from ai_agent_core.opencode_ui.protocol import PREFIX, directory


def base(scope):
    return PREFIX + "/agents/" + directory(scope)


def opened(scope, key="open-bridge"):
    return dict(
        type="work.session.open",
        request_id=key,
        target="work",
        scope=scope.model_dump(mode="json"),
        arguments=dict(schema_id="work.session.open-args", data={"title": "A study"}),
    )


def message(scope, identifier, *, text="A synthetic question", key="say-bridge", version=1):
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


def test_bridge_stopped_intake_replay_cas_scopes_auth_and_deny(tmp_path, monkeypatch):
    async def scenario():
        # Capture the real boundary to prepare a second authorised test profile. This is
        # fixture setup, not a production activation/provisioning endpoint.
        surfaces = []
        original = bootstrap.install_access

        def installed(*args, **kwargs):
            surface = original(*args, **kwargs)
            surfaces.append(surface)
            return surface

        monkeypatch.setattr(bootstrap, "install_access", installed)
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            client, rt = host.env.client, host.runtime
            manifest = await client.get(PREFIX + "/manifest")
            assert manifest.status_code == 200
            assert manifest.json()["native_routes_exposed"] is False
            agents = await client.get(PREFIX + "/agents")
            assert agents.status_code == 200, agents.text
            card = next(c for c in agents.json()["items"] if c["active_runtime"])
            assert card["directory"] == directory(host.scope)
            assert set(card) == {
                "directory",
                "scope",
                "profile_id",
                "profile_version",
                "label",
                "active_runtime",
            }
            first = await command(host, opened(host.scope))
            assert first.status_code == 200, first.text
            assert (await command(host, opened(host.scope))).json() == first.json()
            identifier = first.json()["resource_id"]
            made = await command(host, message(host.scope, identifier))
            assert made.status_code == 200, made.text
            assert (await command(host, message(host.scope, identifier))).json() == made.json()
            changed = await command(host, message(host.scope, identifier, text="Changed"))
            assert changed.status_code == 409
            stale = await command(host, message(host.scope, identifier, key="stale"))
            assert stale.status_code == 409
            shown = await view(host, identifier)
            assert [e["text"] for e in shown["entries"]] == ["A synthetic question"]
            listing = await client.get(base(host.scope) + "/sessions", params={"limit": 1})
            assert listing.status_code == 200
            assert "context_sources" not in listing.text and "pending_reply" not in listing.text
            assert listing.json()["items"][0]["id"] == identifier
            assert model.count() == 0 and rt.supervisor.snapshot.state.value == "STOPPED"

            # Already prepared inactive partition: durable intake, never a second executor.
            inactive = host.scope.model_copy(
                update={"persona_id": "second-person", "memory_space_id": "second-private"}
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
                    "canon": "Second fictional personality",
                    "style": "Concise fictional style",
                    "version": 1,
                }
            )
            await put(
                rt.store,
                rt.registry,
                Owner.PERSONA,
                inactive,
                "seed-second-profile",
                second,
                None,
                rt.readers,
            )
            rt.scopes = (*rt.scopes, inactive)
            surfaces[-1].sessions.scopes = (*surfaces[-1].sessions.scopes, inactive)
            login = await client.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://127.0.0.1:8765"},
                json={"password": paths(host.root).password.read_text().strip()},
            )
            assert login.status_code == 200
            host.env.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            catalogue = (await client.get(PREFIX + "/agents")).json()["items"]
            assert {c["profile_id"] for c in catalogue} >= {host.scope.persona_id, "second-person"}
            assert not next(c for c in catalogue if c["profile_id"] == "second-person")[
                "active_runtime"
            ]
            other = await command(host, opened(inactive, "inactive-open"), selected=inactive)
            assert other.status_code == 200, other.text
            other_id = other.json()["resource_id"]
            deferred = rt.supervisor.clock.elapsed() + 3600
            rt.autonomous_activity.next_check = deferred
            stored = await command(
                host, message(inactive, other_id, key="inactive-message"), selected=inactive
            )
            assert stored.status_code == 200, stored.text
            assert rt.autonomous_activity.next_check == deferred
            assert (await view(host, other_id, inactive))["entries"][0]["text"]
            assert (await client.get(base(host.scope) + "/sessions/" + other_id)).status_code == 403
            assert rt.supervisor.snapshot.scope.partition == host.scope.partition
            assert rt.supervisor.snapshot.state.value == "STOPPED" and model.count() == 0

            foreign = Scope(
                workspace_id=host.scope.workspace_id,
                persona_id="another-person",
                memory_space_id="another-memory",
                memory_mode="work",
            )
            denied = await client.get(base(foreign) + "/sessions/" + identifier)
            assert denied.status_code == 403
            assert (await command(host, opened(foreign, "foreign"))).status_code == 403
            assert (
                await client.get(
                    base(host.scope) + "/sessions", params={"persona_id": "another-person"}
                )
            ).status_code == 400
            assert (
                await command(
                    host, opened(host.scope, "csrf"), headers={"Origin": "http://127.0.0.1:8765"}
                )
            ).status_code == 403
            assert (
                await command(
                    host,
                    opened(host.scope, "origin"),
                    headers={**host.env.headers, "Origin": "https://not-the-platform.invalid"},
                )
            ).status_code == 403
            for suffix in ("pty", "provider", "auth", "config", "prompt_async", "api/session"):
                assert (await client.get(base(host.scope) + "/" + suffix)).status_code == 404
            forbidden = {
                **opened(host.scope, "forbidden"),
                "type": "work.assign",
                "arguments": {
                    "schema_id": "work.assign-args",
                    "data": {"goal": "An arbitrary command", "reason": "No"},
                },
            }
            assert (await command(host, forbidden)).status_code == 403
            client.cookies.clear()
            assert (await client.get(PREFIX + "/agents")).status_code == 401

    asyncio.run(scenario())


def test_bridge_real_owners_one_answer_ack_replay_and_stale_run(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            await install(rt, host.scope)
            fast(rt)
            result = await command(host, opened(host.scope))
            identifier = result.json()["resource_id"]
            assert (await command(host, message(host.scope, identifier))).status_code == 200
            run = rt.supervisor.snapshot
            start = dict(
                type="agent.start",
                request_id="bridge-start",
                target="primary",
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={"autonomy": "autonomous"},
            )
            model.queues["activity"].append({"reply": "A synthetic reply"})
            assert (await command(host, start)).status_code == 200
            pending = await until(
                lambda: view(host, identifier),
                lambda v: v.get("pending_reply") and v["pending_reply"]["acknowledgeable"],
                "bridge reply",
            )
            draft = pending["pending_reply"]
            assert pending["processed_sequence"] == 0
            ack = dict(
                type="dialogue.acknowledge",
                request_id="bridge-ack",
                target=draft["utterance_id"],
                scope=draft["scope"],
                expected_version=draft["version"],
                arguments={"revision": draft["revision"]},
            )
            wrong = {
                **ack,
                "request_id": "wrong-revision",
                "arguments": {"revision": draft["revision"] + 1},
            }
            assert (await command(host, wrong)).status_code == 409
            accepted = await command(host, ack)
            assert accepted.status_code == 200, accepted.text
            assert (await command(host, ack)).json() == accepted.json()
            final = await view(host, identifier)
            assert final["processed_sequence"] == 2 and final["pending_reply"] is None
            assert [e["speaker"] for e in final["entries"]] == ["operator", "character"]
            assert model.count("activity") == 1
            # STOP remains the direct owner path, independent of bridge/catalogue/DB.
            active = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                "/api/v1/agents/" + active.agent_id + "/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="bridge-stop",
                    target=active.agent_id,
                    scope=active.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert (await command(host, {**ack, "request_id": "late-ack"})).status_code >= 400
            assert (await command(host, {**start, "request_id": "stale-start"})).status_code == 409

    asyncio.run(scenario())


def test_session_rename_is_owner_cas_and_timestamps_survive_refetch(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            first = await command(host, opened(host.scope, "rename-open"))
            identifier = first.json()["resource_id"]
            payload = dict(
                type="work.session.rename",
                request_id="rename-once",
                target=identifier,
                scope=host.scope.model_dump(mode="json"),
                expected_version=1,
                arguments=dict(
                    schema_id="work.session.rename-args",
                    data={"session_id": identifier, "title": "A clear title"},
                ),
            )
            renamed = await command(host, payload)
            assert renamed.status_code == 200, renamed.text
            assert (await command(host, payload)).json() == renamed.json()
            conflict = await command(host, dict(payload, request_id="rename-stale"))
            assert conflict.status_code == 409
            one = await host.env.client.get(base(host.scope) + "/sessions")
            two = await host.env.client.get(base(host.scope) + "/sessions")
            assert one.json() == two.json()
            card = one.json()["items"][0]
            assert card["title"] == "A clear title"
            detail = await view(host, identifier)
            assert detail["created_at"] == card["created_at"]
            assert detail["updated_at"] == card["updated_at"]
            assert detail["entries"] == [] and detail["processed_sequence"] == 0
            from ai_agent_contracts.control import Freshness

            from ai_agent_core.m3_composition.activity import book
            from ai_agent_core.m3_composition.work_sessions import sessions

            await command(host, message(host.scope, identifier, version=2))
            session = await sessions(host.runtime).get(host.scope, identifier)
            run_scope = host.scope.model_copy(update={"agent_id": "fixture", "run_id": "run"})
            await book(host.runtime).prepare_session(
                session,
                1,
                "synthetic-active-turn",
                run_scope,
                Freshness(run_id="run", control_epoch=1, profile_version=1, policy_version=1),
                [],
                [],
            )
            busy = await command(host, dict(payload, request_id="busy", expected_version=3))
            assert busy.status_code == 429 and busy.json()["code"] == "RESOURCE_BUSY"
            assert (await view(host, identifier))["title"] == "A clear title"
            assert model.count() == 0 and host.runtime.supervisor.snapshot.state.value == "STOPPED"

    asyncio.run(scenario())
