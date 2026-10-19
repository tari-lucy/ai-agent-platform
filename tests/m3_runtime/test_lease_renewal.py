"""A lapsed 2 s policy proof during a slow renewal write does not cancel a healthy job.

The executor's renewal loop alone, on a stand-in runtime: the same renewal request is replayed
after STALE_CONTEXT; any other refusal, or a proof that keeps failing, still cancels.
"""

import asyncio
from types import SimpleNamespace

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from ai_agent_core.m3_composition.service import RENEW_ATTEMPTS, Execution


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def elapsed(self) -> float:
        self.seconds += 3  # every tick moves past the next renewal time quickly
        return self.seconds


def execution(failures):
    calls = []

    async def renew(worker, request):
        calls.append(request.request_id)
        if failures:
            raise ContractError(failures.pop(0))
        return SimpleNamespace(**{**vars(request.lease), "renewed": len(calls)})

    lease = SimpleNamespace(scope=None, operation_id="op-1")
    ex = object.__new__(Execution)
    ex.runtime = SimpleNamespace(
        supervisor=SimpleNamespace(clock=Clock()),
        jobs=SimpleNamespace(renew=renew),
        worker="worker",
    )
    ex.lease, ex.cancel = lease, asyncio.Event()
    ex.guard = lambda: None
    return ex, calls


async def loop(ex, seconds=0.4):
    task = asyncio.create_task(ex.renew())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def lease_command(monkeypatch):
    import ai_agent_core.m3_composition.service as service

    monkeypatch.setattr(service, "ServiceLeaseCommand", lambda **kw: SimpleNamespace(**kw))


def test_a_lapsed_proof_replays_the_same_renewal_and_the_job_continues(monkeypatch):
    lease_command(monkeypatch)
    ex, calls = execution([ErrorCode.STALE_CONTEXT])
    asyncio.run(loop(ex))
    assert not ex.cancel.is_set() and getattr(ex.lease, "renewed", 0) >= 2
    assert calls[0] == calls[1]  # the same request id: the stored lease is replayed


def test_a_real_refusal_or_a_proof_that_keeps_failing_still_cancels(monkeypatch):
    lease_command(monkeypatch)
    ex, calls = execution([ErrorCode.ACCESS_DENIED])
    asyncio.run(loop(ex))
    assert ex.cancel.is_set() and len(calls) == 1
    ex, calls = execution([ErrorCode.STALE_CONTEXT] * RENEW_ATTEMPTS)
    asyncio.run(loop(ex))
    assert ex.cancel.is_set() and len(calls) == RENEW_ATTEMPTS and len(set(calls)) == 1
