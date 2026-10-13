"""Real SQLite JobOwner + resource budget owner share original supervisor identity."""

import asyncio

import pytest
from ai_agent_application.scheduling import Demand, Lane, ResourceScheduler
from ai_agent_application.telemetry import Outcome, Telemetry
from ai_agent_contracts.api import ControlArguments, ControlCommand
from ai_agent_contracts.states import OperationState, RunState
from ai_agent_contracts.workspace import Capability
from test_jobs import create


@pytest.mark.parametrize("stop,retry", [(False, False), (True, False), (False, True)])
def test_sqlite_job_and_budget_each_require_actual_receipt_without_reopening(tmp_path, stop, retry):
    async def scenario():
        f = await create(tmp_path)
        try:
            f.stop()
            await f.supervisor.flush()
            telemetry = Telemetry(clock=f.time.clock())
            scheduler = ResourceScheduler(f.supervisor, telemetry)
            await f.supervisor.control(
                f.operator,
                ControlCommand(
                    request_id="restart-for-budget-test",
                    type="agent.start",
                    target="synthetic-agent",
                    scope=f.supervisor.snapshot.scope,
                    arguments=ControlArguments(autonomy="autonomous"),
                    expected_version=f.supervisor.snapshot.version,
                ),
            )
            f.scope = f.supervisor.snapshot.scope
            cap = Capability(
                id="synthetic.compute",
                version=1,
                scope=f.scope,
                created_at=f.time.now,
                updated_at=f.time.now,
                adapter_id="synthetic-adapter",
                adapter_version="1",
                input_schema_id="synthetic.empty",
                output_schema_id="synthetic.empty",
                effect="none",
                required_permissions=[],
                resource_ids=["llm"],
                timeout_ms=90000,
                cancellation_supported=True,
                idempotency="supported",
                health="ready",
                setting_references=[],
            )
            scheduler.register(cap, (Demand("llm", 1),), Lane.FOREGROUND)
            claim, command, _ = await f.claimed()
            await f.owner.verify(f.worker, claim.lease)
            request = scheduler.request(command, attempt_id=claim.lease.attempt_id)
            allocation = scheduler.acquire(request)
            assert allocation and allocation.acquired
            scheduler.verify(allocation.lease)
            assert len(f.supervisor.snapshot.unconfirmed_resource_ids) == 2
            if retry:
                failed = await f.owner.accept(
                    f.worker, f.result_request(claim, "FAILED"), transient=True
                )
                assert failed.status == OperationState.WAITING
                scheduler.complete(allocation.lease, Outcome.FAILED)
                previous_attempt = claim.lease.attempt_id
                f.time.advance(f.supervisor, 1.5)
                claim = await f.owner.claim(f.worker, f.claim_request(failed))
                assert claim and claim.acquired and claim.lease.attempt_id != previous_attempt
                await f.owner.verify(f.worker, claim.lease)
                request = scheduler.request(command, attempt_id=claim.lease.attempt_id)
                allocation = scheduler.acquire(request)
                assert allocation and allocation.acquired
                scheduler.verify(allocation.lease)
                assert len(f.supervisor.snapshot.unconfirmed_resource_ids) == 2
            if stop:
                f.stop()
                assert scheduler.decision(request).state.value == "cancelling"
            receipt = f.result_request(claim, "CANCELLED" if stop else "SUCCEEDED")
            job = await f.owner.accept(f.worker, receipt)
            assert job.status == (OperationState.CANCELLED if stop else OperationState.SUCCEEDED)
            assert len(f.supervisor.snapshot.unconfirmed_resource_ids) == 1
            scheduler.complete(allocation.lease, Outcome.CANCELLED if stop else Outcome.SUCCEEDED)
            assert f.supervisor.snapshot.unconfirmed_resource_ids == []
            assert f.supervisor.snapshot.state == (RunState.STOPPED if stop else RunState.ACTIVE)
            assert (await f.owner.claim(f.worker, f.claim_request(job))) is None
        finally:
            await f.db.close()

    asyncio.run(scenario())
