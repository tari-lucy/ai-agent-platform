"""The opt-in live probe's cap is enforced before the fourth HTTP request."""

import asyncio
import runpy
from pathlib import Path

import httpx
import pytest


@pytest.mark.parametrize("limit", (2, 3))
def test_live_lease_probe_caps_actual_http_requests(limit):
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/probes/live_long_turn_lease.py")
    )

    async def scenario():
        sent = []

        async def respond(request):
            sent.append(request)
            return httpx.Response(200, content=b"synthetic")

        wire = module["Counted"](limit)
        await wire.inner.aclose()
        wire.inner = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=wire) as client:
            for _ in range(limit):
                assert (
                    await client.post("https://provider.invalid/v1/chat/completions")
                ).status_code == 200
            with pytest.raises(httpx.TransportError, match="PROBE_CALL_LIMIT"):
                await client.post("https://provider.invalid/v1/chat/completions")
        assert wire.calls == len(sent) == limit

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state,pending,expected",
    [
        ("FAULTED", 0, False),
        ("STOPPED", 0, True),
        ("STOPPING", 1, False),
    ],
)
def test_ready_reply_does_not_hide_unclean_stop(state, pending, expected):
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/probes/live_long_turn_lease.py")
    )
    report = dict(success=True, phases={})
    module["finalize"](report, state, pending)
    assert report["success"] is expected
    assert report["turn_success"] is True
