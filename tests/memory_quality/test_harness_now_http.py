"""The panel's live picture is reachable through the real HTTP surface of a prepared host.

Calling build() directly missed a bug that turned every harness-now request into a 503; only
a request through the surface shows that the route answers.
"""

import asyncio

from quality_support import Model, prepared_host
from test_http import scope_params


def test_harness_now_and_picture_answer_over_http(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        async with host.open():
            await host.start()
            params = scope_params(host.scope)
            now = await host.env.client.get("/api/v1/harness-now", params=params)
            ghost = await host.env.client.get("/api/v1/harness-picture/art--a.jpg", params=params)
            bad = await host.env.client.get("/api/v1/harness-picture/..%2Fsecret.jpg", params=params)
            return now, ghost, bad

    now, ghost, bad = asyncio.run(scenario())
    assert now.status_code == 200, now.text
    body = now.json()
    assert body["enabled"] is False and body["checked_at"] and body["lines"] == []
    # No harness in this host: a picture is refused as an invalid argument, never a server error.
    assert ghost.status_code == 400 and bad.status_code in (400, 404)


def test_persona_text_routes_answer_over_http(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        async with host.open():
            await host.start()
            params = scope_params(host.scope)
            listing = await host.env.client.get("/api/v1/persona-texts", params=params)
            text = await host.env.client.get("/api/v1/persona-text/file:GOALS.md", params=params)
            return listing, text

    listing, text = asyncio.run(scenario())
    assert listing.status_code == 200 and listing.json()["enabled"] is False
    assert text.status_code in (400, 404, 501, 503) and text.status_code != 500


def test_a_text_is_saved_over_http_with_csrf_and_a_stale_save_conflicts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    async def close():
        return None

    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        folder = tmp_path / "persona-files"
        folder.mkdir()
        (folder / "GOALS.md").write_text("старые цели\n", encoding="utf-8")
        settings = SimpleNamespace(guide_file=tmp_path / "guide.md", history_dir=tmp_path / "history")
        async with host.open():
            await host.start()
            host.env.runtime.harness = SimpleNamespace(
                settings=settings, folders=lambda: folder, persona=lambda: host.scope.persona_id, parts=[], close=close
            )
            client, headers = host.env.client, host.env.headers
            params = scope_params(host.scope)
            scope = {k: getattr(host.scope, k) for k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")}
            opened = (await client.get("/api/v1/persona-text/file:GOALS.md", params=params)).json()
            body = {"scope": scope, "id": "file:GOALS.md", "content": "новые цели\n", "base_version": opened["version"]}
            no_csrf = await client.post("/api/v1/persona-text", json=body, headers={"Origin": headers["Origin"]})
            saved = await client.post("/api/v1/persona-text", json=body, headers=headers)
            stale = await client.post("/api/v1/persona-text", json=body, headers=headers)  # same old base
            outside = await client.post(
                "/api/v1/persona-text", json={**body, "id": "file:secret.md"}, headers=headers)
            listing = (await client.get("/api/v1/persona-texts", params=params)).json()
            return opened, no_csrf, saved, stale, outside, listing, folder

    opened, no_csrf, saved, stale, outside, listing, folder = asyncio.run(scenario())
    assert opened["content"] == "старые цели\n" and len(opened["version"]) == 64
    assert no_csrf.status_code in (401, 403)  # a mutation without the CSRF token is refused
    assert saved.status_code == 200, saved.text
    assert (folder / "GOALS.md").read_text(encoding="utf-8") == "новые цели\n"
    assert saved.json()["version"] != opened["version"] and len(saved.json()["history"]) == 1
    assert stale.status_code == 409, stale.text  # the version moved on: a conflict, not an overwrite
    assert outside.status_code == 400  # not on the whitelist
    assert any(item["id"] == "file:GOALS.md" for item in listing["items"])
