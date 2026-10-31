"""Crash recovery confirms the harness's resources instead of leaving the host FAULTED."""

import asyncio
from types import SimpleNamespace

from ai_agent_core.m3_composition import policy


def readiness():
    return policy.Readiness(SimpleNamespace())  # type: ignore[arg-type]


def previous(*ids):
    return SimpleNamespace(unconfirmed_resource_ids=list(ids))


def test_a_harness_left_by_a_dead_process_is_swept_once_and_confirmed(monkeypatch):
    sweeps, others = [], []

    async def sweep() -> bool:
        sweeps.append(1)
        return True

    async def reclaim(resource_id: str) -> bool:
        others.append(resource_id)
        return True

    monkeypatch.setattr("ai_agent_adapters.sandbox.agent_gateway.reclaim_harness", sweep)
    monkeypatch.setattr("ai_agent_adapters.sandbox.docker.reclaim", reclaim)
    ids = ("harness-" + "a" * 32, "harness-" + "b" * 32, "warm-aw-task-" + "c" * 32)
    unknown = asyncio.run(readiness().reconcile(previous(*ids)))  # type: ignore[arg-type]
    assert unknown == () and sweeps == [1]  # one sweep covers every harness id
    assert others == ["warm-aw-task-" + "c" * 32]  # other ids keep their own path


def test_an_unreachable_docker_leaves_the_harness_unconfirmed(monkeypatch):
    async def sweep() -> bool:
        return False

    monkeypatch.setattr("ai_agent_adapters.sandbox.agent_gateway.reclaim_harness", sweep)
    ids = ("harness-" + "a" * 32,)
    unknown = asyncio.run(readiness().reconcile(previous(*ids)))  # type: ignore[arg-type]
    assert unknown == ids  # never claim a recovery that Docker could not confirm
