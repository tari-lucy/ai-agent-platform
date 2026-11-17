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
