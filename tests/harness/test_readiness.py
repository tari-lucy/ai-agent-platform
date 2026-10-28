"""No Docker/model: readiness and STOP while OpenCode is still booting."""

import asyncio
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import RunState

from ai_agent_core.m3_composition import harness_host
from ai_agent_core.m3_composition.harness_host import HarnessHost


def host():
    instance = HarnessHost.__new__(HarnessHost)
    instance.closed = False
    instance.ready = False
    instance.rt = SimpleNamespace(
        supervisor=SimpleNamespace(snapshot=SimpleNamespace(state=RunState.ACTIVE, control_epoch=7))
    )
    return instance


def test_waits_for_http_health_not_container_address():
    async def scenario():
        instance = host()
        calls = []

        async def request(method, path, *, timeout):
            calls.append((method, path, timeout))
            assert not instance.ready
            if len(calls) == 1:
                raise ConnectionResetError()
            if len(calls) == 2:
                return {"healthy": False}
            return {"healthy": True}

        instance._request_opencode = request
        await instance._wait_ready(7)
        assert len(calls) == 3
        assert all(method == "GET" and path == "/global/health" for method, path, _ in calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["stop", "epoch", "closed"])
def test_stop_or_epoch_during_health_never_publishes_ready(change):
    async def scenario():
        instance = host()

        async def request(method, path, *, timeout):
            if change == "stop":
                instance.rt.supervisor.snapshot.state = RunState.STOPPED
            elif change == "epoch":
                instance.rt.supervisor.snapshot.control_epoch = 8
            else:
                instance.closed = True
            return {"healthy": True}

        instance._request_opencode = request
        with pytest.raises(ContractError):
            await instance._wait_ready(7)
        assert not instance.ready

    asyncio.run(scenario())


def test_hanging_health_has_wall_time_bound(monkeypatch):
    async def scenario():
        instance = host()

        async def request(method, path, *, timeout):
            await asyncio.sleep(10)

        monkeypatch.setattr(harness_host, "HTTP_STARTUP_TIMEOUT", 0.02)
        instance._request_opencode = request
        with pytest.raises(OSError, match="HARNESS_HTTP_NOT_READY"):
            await asyncio.wait_for(instance._wait_ready(7), timeout=1)
        assert not instance.ready

    asyncio.run(scenario())
