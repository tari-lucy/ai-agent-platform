"""Real Docker with deterministic admission latency; no model or external network."""

import asyncio
import os

import pytest
from test_docker import World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_SANDBOX_IMAGE_ID"), reason="explicit local Docker image ID required"
)


def delayed_monitor(w, monkeypatch, seconds):
    attached = asyncio.Event()
    checking = asyncio.Event()
    ended = asyncio.Event()
    attach = w.runner._attach
    verify = w.runner._verify
    delayed = False

    async def start(*args):
        attached.set()
        return await attach(*args)

    async def check(*args):
        nonlocal delayed
        if attached.is_set() and not delayed:
            delayed = True
            checking.set()
            try:
                await asyncio.sleep(seconds)
            finally:
                ended.set()
        await verify(*args)

    monkeypatch.setattr(w.runner, "_attach", start)
    monkeypatch.setattr(w.runner, "_verify", check)
    return checking, ended


def test_slow_admission_does_not_invent_unknown_outcome(monkeypatch):
    async def scenario():
        w = World()
        checking, ended = delayed_monitor(w, monkeypatch, 1.4)
        result = await w.runner.run(
            w.admission,
            w.task(
                "import time,pathlib; time.sleep(2); "
                "pathlib.Path('outputs/result.txt').write_text('synthetic')"
            ),
        )
        assert checking.is_set() and ended.is_set()
        assert result.status == "succeeded", result
        assert await w.runner.staged(w.admission, result.outputs[0]) == b"synthetic"
        assert w.names[-1][1] == "removed"
        await w.runner.purge()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["stop", "deadline"])
def test_pending_admission_keeps_stop_and_deadline(monkeypatch, reason):
    async def scenario():
        w = World()
        checking, ended = delayed_monitor(w, monkeypatch, 30)
        running = asyncio.create_task(
            w.runner.run(w.admission, w.task("import time; time.sleep(30)"))
        )
        await asyncio.wait_for(checking.wait(), 20)
        if reason == "stop":
            w.alive = False
        result = await asyncio.wait_for(running, 8)
        assert result.status == ("cancelled" if reason == "stop" else "timeout"), result
        assert not result.outputs and ended.is_set()
        assert w.names[-1][1] == "removed"

    asyncio.run(scenario())
