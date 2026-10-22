"""Shutdown must not interrupt the factual job receipt after executor termination."""

import asyncio

from ai_agent_contracts.states import OperationState
from composition_support import run, setup, stop

from ai_agent_core.m3_composition.schemas import TaskInput


def test_shutdown_during_factual_receipt_settles_the_same_job(tmp_path, monkeypatch):
    async def scenario():
        env = await setup(tmp_path)
        rt = env.runtime
        entered, release = asyncio.Event(), asyncio.Event()
        accept = rt.jobs.accept

        async def completed(execution):
            return []

        async def held_receipt(*args, **kwargs):
            entered.set()
            await release.wait()
            return await accept(*args, **kwargs)

        monkeypatch.setattr(rt, "_perform", completed)
        monkeypatch.setattr(rt.jobs, "accept", held_receipt)
        episode = await env.world.episode()
        job = await rt.submit(
            env.authority,
            "receipt-race",
            TaskInput(kind="reflect", trigger_id="synthetic", episode_ids=[episode]),
        )
        rt._active = asyncio.create_task(rt.execute(job))
        await asyncio.wait_for(entered.wait(), 5)
        stop(env)
        closing = asyncio.create_task(rt.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 5)
        stored, _ = await rt.jobs.store.load(job.operation_id, job.scope)
        assert stored.status == OperationState.CANCELLED
        assert not rt.supervisor.snapshot.unconfirmed_resource_ids
        assert not env.world.provider.calls

    run(scenario())
