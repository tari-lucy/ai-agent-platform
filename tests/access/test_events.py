"""Same-loop WebSocket authorization, revocation and bounded canonical fan-out."""

import asyncio
import json

import pytest
from access_support import WORKER_TOKEN, Harness, Socket
from ai_agent_contracts.api import PanelEvent
from operations_support import BASE

from ai_agent_core.access import AccessLimits


def event(h, scope=BASE, resource="synthetic-resource", identifier="synthetic-event"):
    return PanelEvent(
        id=identifier,
        event_id=identifier,
        scope=scope,
        created_at=h.time.now,
        type="run.changed",
        resource_id=resource,
        resource_version=1,
        occurred_at=h.time.now,
        causal_id=None,
        operation_id=None,
    )


@pytest.mark.parametrize(
    "headers,query",
    [
        ({"cookie": None}, b""),
        ({"cookie": "ai_operator_session=" + "x" * 43}, b""),
        ({"origin": None}, b""),
        ({"origin": "https://evil.invalid"}, b""),
        ({"host": "evil.invalid"}, b""),
        ({"authorization": "Bearer " + WORKER_TOKEN}, b""),
        ({}, b"token=" + WORKER_TOKEN.encode()),
    ],
)
def test_unauthorized_websocket_never_accepts(headers, query):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        socket = await Socket().setup(h, headers=headers, query=query)
        assert (await socket.next())["type"] == "websocket.close"
        assert not h.surface.events._clients
        await socket.close()
        await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "foreign"},
        {"persona_id": "foreign"},
        {"memory_space_id": "foreign"},
        {"memory_mode": "work"},
    ],
)
def test_foreign_subscription_closes_before_any_event(change):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        socket = await Socket().setup(h)
        assert (await socket.next())["type"] == "websocket.accept"
        await socket.subscribe(BASE.model_copy(update=change))
        message = await socket.next()
        assert message["type"] == "websocket.close"
        assert not h.surface.events._clients
        await socket.close()
        await h.close()

    asyncio.run(scenario())


def test_event_resource_run_scope_filters_and_logout_revocation():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        socket = await Socket().setup(h)
        assert (await socket.next())["type"] == "websocket.accept"
        selected = h.supervisor.snapshot.scope
        await socket.subscribe(selected, resource_ids=["chosen"])
        for foreign in (
            BASE.model_copy(update={"persona_id": "foreign"}),
            selected.model_copy(update={"run_id": "foreign-run"}),
            selected.model_copy(update={"memory_mode": "work"}),
        ):
            h.surface.events.publish(event(h, foreign, resource="chosen"))
        h.surface.events.publish(event(h, selected, resource="wrong"))
        h.surface.events.publish(event(h, selected, resource="chosen"))
        message = await socket.next()
        assert message["type"] == "websocket.send"
        payload = json.loads(message["text"])
        assert payload["resource_id"] == "chosen"
        assert payload["scope"] == selected.model_dump(mode="json")
        assert (await h.post("/api/v1/auth/logout")).status_code == 204
        assert (await socket.next())["type"] == "websocket.close"
        assert not h.surface.events._clients
        await socket.close()
        await h.close()

    asyncio.run(scenario())


def test_slow_client_and_fanout_capacity_are_finite():
    async def scenario():
        h = await Harness().setup(limits=AccessLimits(websocket_clients=2, websocket_events=2))
        await h.login()
        slow = await Socket().setup(h)
        await slow.next()
        await slow.subscribe()
        # No await between publishing: simulate producer faster than drain.
        for index in range(3):
            h.surface.events.publish(event(h, identifier="event-" + str(index)))
        message = await slow.next()
        assert message["type"] == "websocket.close" and message["code"] == 1013
        assert not h.surface.events._clients
        a, b = [await Socket().setup(h) for _ in range(2)]
        for socket in (a, b):
            assert (await socket.next())["type"] == "websocket.accept"
            await socket.subscribe()
        c = await Socket().setup(h)
        assert (await c.next())["code"] == 1013
        assert len(h.surface.events._clients) == 2
        for socket in (a, b, c, slow):
            await socket.close()
        await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("extra", [{"after_event_id": "old-event"}, {"replay_limit": 257}])
def test_reconnect_gap_does_not_repeat_model_calls(extra):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        socket = await Socket().setup(h)
        await socket.next()
        await socket.subscribe(**extra)
        assert (await socket.next())["type"] == "websocket.close"
        assert not h.surface.events._clients
        await socket.close()
        await h.close()

    asyncio.run(scenario())


def test_websocket_cannot_submit_commands_and_idle_expiry_is_enforced():
    async def scenario():
        h = await Harness().setup(limits=AccessLimits(session_seconds=20, idle_seconds=5))
        await h.login()
        first = await Socket().setup(h)
        await first.next()
        await first.subscribe()
        epoch = h.supervisor.snapshot.control_epoch
        await first.incoming.put({"type": "websocket.receive", "text": json.dumps(h.stop())})
        assert (await first.next())["type"] == "websocket.close"
        assert h.supervisor.snapshot.control_epoch == epoch
        await first.close()
        second = await Socket().setup(h)
        await second.next()
        await second.subscribe()
        h.time.advance(5)
        assert (await second.next())["type"] == "websocket.close"
        await second.close()
        await h.close()

    asyncio.run(scenario())


def test_authorized_websocket_receives_real_supervisor_stop_event():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        socket = await Socket().setup(h)
        assert (await socket.next())["type"] == "websocket.accept"
        await socket.subscribe(h.supervisor.snapshot.scope)
        response = await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop())
        assert response.status_code == 200
        message = await socket.next()
        event = json.loads(message["text"])
        assert event["type"] == "run.changed"
        assert event["resource_version"] == h.supervisor.snapshot.version
        assert event["scope"] == h.supervisor.snapshot.scope.model_dump(mode="json")
        assert h.cookie not in message["text"] and h.csrf not in message["text"]
        await socket.close()
        await h.close()

    asyncio.run(scenario())
