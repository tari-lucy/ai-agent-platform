"""Synthetic credentials/scope and same-loop ASGI harness; no listener or real .env."""

import asyncio
import json
import secrets
from functools import lru_cache

import httpx
from ai_agent_adapters.configuration import BootstrapSettings
from fastapi import FastAPI
from operations_support import BASE, Fixture
from pydantic import SecretStr

from ai_agent_core.access import AccessPorts, install_access, password_hash
from ai_agent_core.access.sessions import COOKIE

ORIGIN = "http://127.0.0.1:8765"
PASSWORD = "synthetic-password-not-a-local-binding"
WORKER_TOKEN = secrets.token_hex(32)


@lru_cache
def configured_hash():
    return password_hash(SecretStr(PASSWORD))


class Harness:
    async def setup(self, *, ports=None, origin=ORIGIN, limits=None, fixture=None):
        self.fixture = fixture or await Fixture().setup()
        self.supervisor = self.fixture.supervisor
        self.time = self.fixture.time
        self.app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)
        self.origin = origin
        self.settings = BootstrapSettings(
            operator_origin=origin,
            operator_password_hash=configured_hash(),
            worker_service_token=SecretStr(WORKER_TOKEN),
        )
        self.surface = install_access(
            self.app,
            self.supervisor,
            self.settings,
            ports or AccessPorts(extensions=self.fixture.registry, limits=limits),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=origin,
        )
        self.csrf = None
        return self

    async def login(self, **kwargs):
        response = await self.client.post(
            "/api/v1/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": self.origin},
            **kwargs,
        )
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        self.cookie = self.client.cookies.get(COOKIE)
        return response

    async def post(self, path, *, data=None, headers=None):
        bindings = {"Origin": self.origin, "X-CSRF-Token": self.csrf or ""}
        bindings.update(headers or {})
        return await self.client.post(path, json=data, headers=bindings)

    def scope_query(self, scope=BASE, **extra):
        return {
            **dict(
                zip(
                    ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
                    scope.partition,
                    strict=True,
                )
            ),
            **extra,
        }

    def stop(self, scope=None, request_id="synthetic-stop"):
        return {
            "type": "agent.stop",
            "request_id": request_id,
            "scope": (scope or self.supervisor.snapshot.scope).model_dump(mode="json"),
            "target": "synthetic-agent",
        }

    async def close(self):
        await self.client.aclose()


class Socket:
    async def setup(self, harness, *, headers=None, query=b""):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        fields = {
            "host": harness.origin.split("://")[1],
            "origin": harness.origin,
            "cookie": COOKIE + "=" + harness.cookie,
        }
        fields.update(headers or {})
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": query,
            "headers": [(k.encode(), v.encode()) for k, v in fields.items() if v is not None],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 8765),
            "subprotocols": [],
        }
        await self.incoming.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(
            harness.app(self.scope, self.incoming.get, self.outgoing.put)
        )
        return self

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def subscribe(self, scope=BASE, **extra):
        await self.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps({"scope": scope.model_dump(mode="json"), **extra}),
            }
        )
        await asyncio.sleep(0.02)

    async def close(self):
        if not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 2)
