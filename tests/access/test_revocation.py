"""Logout closes a pending start, shutdown closes sessions, STOP transport is bounded."""

import asyncio
import json

import pytest
from access_support import ORIGIN, PASSWORD, Harness
from ai_agent_contracts.codec import ContractError
from pydantic import SecretStr

from ai_agent_core.access import AccessLimits


def test_real_operator_logout_while_start_waits_cannot_open_admission():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        assert (
            await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop())
        ).status_code == 200
        await h.supervisor.flush()
        readiness = h.supervisor.readiness
        entered, release = asyncio.Event(), asyncio.Event()

        class Delayed:
            async def check(self, run):
                entered.set()
                await release.wait()
                return await readiness.check(run)

            async def reconcile(self, previous):
                return ()

        h.supervisor.readiness = Delayed()
        run = h.supervisor.snapshot
        start = asyncio.create_task(
            h.post(
                "/api/v1/commands",
                data={
                    "type": "agent.start",
                    "request_id": "restart",
                    "target": "synthetic-agent",
                    "scope": run.scope.model_dump(mode="json"),
                    "expected_version": run.version,
                    "arguments": {"autonomy": "autonomous"},
                },
            )
        )
        await entered.wait()
        assert (await h.post("/api/v1/auth/logout")).status_code == 204
        release.set()
        response = await start
        assert response.status_code == 403
        assert h.supervisor.snapshot.state == "FAULTED"
        with pytest.raises(ContractError):
            h.supervisor.admit("activity")
        await h.close()

    asyncio.run(scenario())


def test_shutdown_revokes_sessions_services_and_pending_login():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        h.surface.sessions.shutdown()
        assert (await h.client.get("/api/v1/auth/session")).status_code == 401
        with pytest.raises(ContractError):
            await h.surface.sessions.login(SecretStr(PASSWORD))
        await h.close()

    asyncio.run(scenario())


def test_slow_stop_bodies_have_separate_finite_capacity():
    async def scenario():
        h = await Harness().setup(limits=AccessLimits(sessions=2))
        await h.login()
        incoming = asyncio.Event()
        raw = json.dumps(h.stop()).encode()
        replies = []

        async def receive():
            await incoming.wait()
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message):
            replies.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "http",
            "path": "/api/v1/agents/synthetic-agent/stop",
            "raw_path": b"/api/v1/agents/synthetic-agent/stop",
            "method": "POST",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"origin", ORIGIN.encode()),
                (b"cookie", ("ai_operator_session=" + h.cookie).encode()),
                (b"x-csrf-token", h.csrf.encode()),
                (b"content-type", b"application/json"),
            ],
        }
        tasks = [asyncio.create_task(h.app(scope, receive, send)) for _ in range(2)]
        while h.surface._stops != 2:
            await asyncio.sleep(0.01)
        response = await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop())
        assert response.status_code == 429
        # STOP requests do not consume or wait behind the general request queue.
        assert h.surface._requests == 0
        incoming.set()
        await asyncio.gather(*tasks)
        assert h.surface._stops == 0
        assert h.supervisor.snapshot.state == "STOPPED"
        assert all(m["status"] == 200 for m in replies if m["type"] == "http.response.start")
        await h.close()

    asyncio.run(scenario())
