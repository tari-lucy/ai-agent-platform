"""The live probe refuses another HTTP call before sending it; no network or credentials."""

import asyncio
import runpy
from pathlib import Path

import httpx
import pytest


def test_live_probe_wire_cap_sends_no_extra_request():
    counter = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/probes/workspace_v2_live.py")
    )["Counted"]

    async def scenario():
        sent = []
        wire = counter(2)
        await wire.inner.aclose()
        wire.inner = httpx.MockTransport(
            lambda request: sent.append(request.url.path) or httpx.Response(200)
        )
        async with httpx.AsyncClient(transport=wire) as client:
            await client.get("https://synthetic.invalid/one")
            await client.get("https://synthetic.invalid/two")
            with pytest.raises(httpx.TransportError, match="PROBE_CALL_LIMIT"):
                await client.get("https://synthetic.invalid/three")
        assert sent == ["/one", "/two"] and wire.calls == 2

    asyncio.run(scenario())
