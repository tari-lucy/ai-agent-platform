"""Sealed frontend files and real owner SSE. All data/builds are synthetic."""

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from access_support import Harness
from ai_agent_contracts.api import StreamChunk
from test_events import event

from ai_agent_core.access.sessions import COOKIE
from ai_agent_core.local_host.layout import HostError
from ai_agent_core.local_host.process import open_when_ready
from ai_agent_core.opencode_ui.assets import BASE, MANIFEST, load, mount, prepare
from ai_agent_core.opencode_ui.protocol import PREFIX, directory


def build(tmp_path):
    root = tmp_path / "ui"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<html><head><script type="module" src="/opencode/assets/app.js"></script>'
        '<link rel="stylesheet" href="/opencode/assets/app.css"></head>'
        '<body><div id="root">Synthetic build only</div></body></html>'
    )
    (root / "assets/app.js").write_text("document.documentElement.dataset.loaded='yes'")
    (root / "assets/app.css").write_text("body{color:black}")
    return root


def test_sealed_assets_scope_api_and_no_live_filesystem(tmp_path):
    async def scenario():
        root = build(tmp_path)
        prepare(root)
        bundle = load(root, tmp_path / "private-host")
        # Only bytes in the checked manifest are served, including after disk mutation.
        (root / ".env").write_text("NOT-A-REAL-SECRET")
        (root / "data.sqlite").write_text("PRIVATE-MARKER")
        (root / "assets/app.js").write_text("CHANGED-UNTRUSTED-MARKER")
        h = await Harness().setup()
        try:
            mount(h.app, bundle)
            page = await h.client.get(BASE + "/")
            assert page.status_code == 200 and "Synthetic build" in page.text
            assert "connect-src 'self'" in page.headers["content-security-policy"]
            assert page.headers["cache-control"] == "no-store"
            js = await h.client.get(BASE + "/assets/app.js")
            assert js.status_code == 200 and "CHANGED" not in js.text
            assert "javascript" in js.headers["content-type"]
            deep = await h.client.get(
                BASE + "/" + directory(h.surface.sessions.scopes[0]) + "/session/logical-session"
            )
            assert deep.status_code == 200 and deep.text == page.text
            for path in (
                "/.env",
                "/data.sqlite",
                "/" + MANIFEST,
                "/api/session",
                "/session",
                "/provider",
                "/auth",
                "/pty",
                "/assets/missing.js",
                "/%2eenv",
            ):
                assert (await h.client.get(BASE + path)).status_code in (400, 404), path
            assert (
                await h.client.get(BASE + "/assets/app.js", params={"x": "1"})
            ).status_code == 400
            assert (
                await h.client.post(BASE + "/", headers={"Origin": h.origin})
            ).status_code == 405
            assert (await h.client.get(PREFIX + "/manifest")).status_code == 401
            await h.login()
            assert (await h.client.get(PREFIX + "/manifest")).status_code == 200
        finally:
            await h.close()
        with pytest.raises(HostError, match="OPENCODE_UI_"):
            load(root, tmp_path / "private-host")

    asyncio.run(scenario())


def test_bad_build_and_overlap_refused(tmp_path):
    root = build(tmp_path)
    (root / "index.html").write_text('<script src="https://external.invalid/app.js"></script>')
    with pytest.raises(HostError, match="BASE_MISMATCH"):
        prepare(root)
    (root / "index.html").write_text('<script src="/assets/app.js"></script>')
    with pytest.raises(HostError, match="BASE_MISMATCH"):
        prepare(root)
    (root / "index.html").write_text('<script src="/opencode/assets/app.js"></script>')
    prepare(root)
    for host in (root, root / "data", tmp_path):
        with pytest.raises(HostError, match="OVERLAPS_HOST"):
            load(root, host)
    manifest = json.loads((root / MANIFEST).read_text())
    manifest["files"]["../outside.js"] = manifest["files"]["assets/app.js"]
    (root / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(HostError, match="INVALID"):
        load(root, tmp_path / "private-host")


def test_missing_ui_is_not_mistaken_for_the_old_panel():
    async def scenario():
        h = await Harness().setup()
        try:
            mount(h.app, None)
            response = await h.client.get(BASE + "/")
            assert response.status_code == 503
            assert response.json()["code"] == "CAPABILITY_UNAVAILABLE"
        finally:
            await h.close()

    asyncio.run(scenario())


class Connection:
    async def open(self, h, *, selected=None, headers=None):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.h = h
        values = {
            "host": h.origin.split("://")[1],
            "origin": h.origin,
            "cookie": COOKIE + "=" + h.cookie,
        }
        values.update(headers or {})
        path = PREFIX + "/agents/" + directory(selected or h.surface.sessions.scopes[0]) + "/events"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "scheme": "http",
            "http_version": "1.1",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in values.items() if v is not None],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 8765),
        }
        await self.incoming.put({"type": "http.request", "body": b"", "more_body": False})
        self.task = asyncio.create_task(h.app(scope, self.incoming.get, self.outgoing.put))
        return self

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 3)

    async def close(self):
        await self.incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 3)


def test_sse_isolation_no_private_stream_logout_and_no_replay():
    async def scenario():
        h = await Harness().setup()
        try:
            await h.login()
            conn = await Connection().open(h, headers={"last-event-id": "old-event"})
            assert (await conn.next())["status"] == 200
            ready = (await conn.next())["body"]
            assert b'"replay":"refetch"' in ready and b"old-event" not in ready
            selected = h.surface.sessions.scopes[0]
            private = selected.model_copy(update={"agent_id": "synthetic-agent", "run_id": "run"})
            now = h.time.now
            h.surface.events.private_stream(
                StreamChunk(
                    id="chunk",
                    scope=private,
                    created_at=now,
                    type="generation.chunk",
                    generation_id="generation",
                    utterance_id="utterance",
                    attempt_id="attempt",
                    sequence=1,
                    data_type="text",
                    data="PRIVATE-STREAM-MARKER",
                )
            )
            h.surface.events.publish(
                event(
                    h,
                    scope=selected.model_copy(update={"persona_id": "other"}),
                    identifier="foreign-event",
                )
            )
            h.surface.events.publish(event(h, scope=selected, identifier="owned-event"))
            raw = (await conn.next())["body"]
            assert b"owned-event" in raw and b"PRIVATE" not in raw and b"foreign" not in raw
            assert h.surface._requests == 0  # UI connection holds no ordinary request slot
            logout = await h.post("/api/v1/auth/logout")
            assert logout.status_code == 204
            closed = (await conn.next())["body"]
            assert b"event: closed" in closed and b"ACCESS_DENIED" in closed
            assert (await conn.next())["more_body"] is False
            await conn.close()
            assert not h.surface.events._clients
        finally:
            await h.close()

    asyncio.run(scenario())


def test_sse_foreign_scope_and_client_ceiling_cleanup():
    async def scenario():
        h = await Harness().setup()
        try:
            await h.login()
            selected = h.surface.sessions.scopes[0]
            foreign = selected.model_copy(update={"persona_id": "foreign"})
            conn = await Connection().open(h, selected=foreign)
            assert (await conn.next())["status"] == 403
            await conn.close()
            # Existing broker limit is shared by SSE and WebSocket subscribers.
            h.surface.events.limits = replace(h.surface.events.limits, websocket_clients=1)
            first = await Connection().open(h)
            assert (await first.next())["status"] == 200
            await first.next()
            second = await Connection().open(h)
            assert (await second.next())["status"] == 429
            await second.close()
            await first.close()
            assert not h.surface.events._clients
            third = await Connection().open(h)
            assert (await third.next())["status"] == 200
            await third.next()
            h.time.advance(h.surface.limits.idle_seconds + 1)
            assert b"ACCESS_DENIED" in (await third.next())["body"]
            await third.close()
        finally:
            await h.close()

    asyncio.run(scenario())


def test_browser_launcher_selects_only_known_local_entry(monkeypatch):
    seen = []
    monkeypatch.setattr("webbrowser.open", lambda url, **kwargs: seen.append(url) or True)
    open_when_ready(SimpleNamespace(started=True), 8765, threading.Event(), "/opencode/")
    assert seen == ["http://127.0.0.1:8765/opencode/"]
    with pytest.raises(HostError):
        open_when_ready(
            SimpleNamespace(started=True), 8765, threading.Event(), "//external.invalid"
        )
