"""The idle monitor yields to IO; action proofs and cheap STOP never use a cached grant."""

import asyncio
import time
from dataclasses import replace

import pytest
from ai_agent_contracts.codec import ContractError
from test_docker import World


def world(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", "sha256:" + "a" * 64)
    return World()  # _watch only: no Docker process, model or filesystem IO


def test_monitor_does_not_repeat_finished_proof_on_every_stop_poll(monkeypatch):
    async def scenario():
        w = world(monkeypatch)
        calls = 0
        verify = w.runner._verify

        async def counted(*args):
            nonlocal calls
            calls += 1
            await verify(*args)

        monkeypatch.setattr(w.runner, "_verify", counted)
        attached = asyncio.create_task(asyncio.sleep(0.45, result=b"done"))
        result = await w.runner._watch(
            attached, w.admission, w.record, asyncio.Event(), time.monotonic() + 3
        )
        assert result is None and calls == 1

    asyncio.run(scenario())


def test_stop_is_observed_between_monitor_proofs(monkeypatch):
    async def scenario():
        w = world(monkeypatch)
        ready = asyncio.Event()
        original = w.runner._verify

        async def verified(*args):
            await original(*args)
            ready.set()

        monkeypatch.setattr(w.runner, "_verify", verified)
        attached = asyncio.create_task(asyncio.sleep(30, result=b"done"))
        watch = asyncio.create_task(
            w.runner._watch(attached, w.admission, w.record, asyncio.Event(), time.monotonic() + 10)
        )
        try:
            await asyncio.wait_for(ready.wait(), 1)
            w.alive = False
            result = await asyncio.wait_for(watch, 0.5)
            assert result.status == "cancelled"
        finally:
            attached.cancel()
            await asyncio.gather(attached, return_exceptions=True)

    asyncio.run(scenario())


def test_revocation_is_fresh_at_action_boundary_within_monitor_interval(monkeypatch):
    async def scenario():
        w = world(monkeypatch)
        await w.runner._verify(w.admission, w.record)
        w.revoked = True
        with pytest.raises(ContractError):
            await w.runner._verify(w.admission, w.record)

    asyncio.run(scenario())


def test_background_proof_rechecks_revocation_without_an_action(monkeypatch):
    async def scenario():
        w = world(monkeypatch)
        template = next(iter(w.runner.templates.values()))
        w.runner.templates[template.id] = replace(template, monitor_interval_seconds=0.1)
        ready = asyncio.Event()
        original = w.runner._verify

        async def verified(*args):
            await original(*args)
            ready.set()

        monkeypatch.setattr(w.runner, "_verify", verified)
        attached = asyncio.create_task(asyncio.sleep(30, result=b"done"))
        watch = asyncio.create_task(
            w.runner._watch(attached, w.admission, w.record, asyncio.Event(), time.monotonic() + 10)
        )
        try:
            await asyncio.wait_for(ready.wait(), 1)
            w.revoked = True
            result = await asyncio.wait_for(watch, 0.5)
            assert result.status == "cancelled"
        finally:
            attached.cancel()
            await asyncio.gather(attached, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, 0.09, 1.01, True, float("nan"), float("inf"), "1"])
def test_monitor_interval_is_bounded(monkeypatch, value):
    w = world(monkeypatch)
    with pytest.raises(ValueError, match="RUNNER_TEMPLATE_INVALID"):
        replace(next(iter(w.runner.templates.values())), monitor_interval_seconds=value)
