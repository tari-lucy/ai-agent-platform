"""Synthetic commands/executors; real transactional SQLite. No live LLM or personal data."""

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from ai_agent_adapters.jobs import schema as jobsdb
from ai_agent_adapters.jobs.store import SQLiteJobs
from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import Clock, ContextApproval, ControlAuthority
from ai_agent_application.jobs.owner import JobOwner, OutboxDispatcher
from ai_agent_application.jobs.ports import ExecutionPolicy, JobLimits, WorkerAuthority
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import (
    ControlArguments,
    ControlCommand,
    Receipt,
    ServiceClaimCommand,
    ServiceLeaseCommand,
    ServiceResultCommand,
    StopCommand,
)
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import Contract, ExtensionPayload, Scope
from ai_agent_contracts.control import ExecutionCommand, ExecutionResult, OperationProjection
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import ErrorCode, OperationState, RunState
from pydantic import TypeAdapter
from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[2]
JOBS = Principal("synthetic-jobs", "core_owner", Owner.JOBS)
BEHAVIOR = Principal("synthetic-behavior", "core_owner", Owner.BEHAVIOR)
BASE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="synthetic-persona",
    memory_space_id="synthetic-test",
    memory_mode="test",
)


class Empty(Contract):
    pass


class Time:
    def __init__(self):
        self.elapsed = 0.0
        self.now = datetime(2026, 10, 13, tzinfo=UTC)

    def clock(self):
        return Clock(lambda: self.elapsed, lambda: self.now)

    def advance(self, owner, seconds):
        while seconds > 0:
            step = min(seconds, 4)
            self.elapsed += step
            self.now += timedelta(seconds=step)
            owner.poll()
            seconds -= step


class Journal:
    async def load(self):
        return []

    async def save(self, value):
        pass


class Readiness:
    def __init__(self, time):
        self.time = time

    async def check(self, run):
        return ContextApproval(run.scope, 1, 1, self.time.elapsed + 60)

    async def reconcile(self, run):
        return ()


class Policy:
    def __init__(self, time, *, retry_safe=True):
        self.time, self.retry_safe = time, retry_safe
        self.denied = False
        self.block = None
        self.entered = asyncio.Event()

    async def check(self, command, worker):
        self.entered.set()
        if self.block:
            await self.block.wait()
        if self.denied:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return ExecutionPolicy(command.id, command.scope, self.retry_safe, self.time.elapsed + 10)


class Fixture:
    async def setup(self, tmp_path, *, limits=None, retry_safe=True):
        self.time = Time()
        self.supervisor = Supervisor(
            BASE,
            "synthetic-agent",
            Journal(),
            Readiness(self.time),
            clock=self.time.clock(),
        )
        self.operator = ControlAuthority("synthetic-operator", frozenset({BASE.partition}), 3600)
        await self.supervisor.recover()
        await self.supervisor.control(
            ControlAuthority(
                "synthetic-operator",
                frozenset({BASE.partition}),
                3600,
            ),
            ControlCommand(
                request_id="start",
                type="agent.start",
                target="synthetic-agent",
                scope=self.supervisor.snapshot.scope,
                arguments=ControlArguments(autonomy="autonomous"),
                expected_version=self.supervisor.snapshot.version,
            ),
        )
        self.scope = self.supervisor.snapshot.scope
        self.registry = ExtensionRegistry()
        self.registry.register("synthetic.empty", Empty)
        self.empty = from_wire(
            TypeAdapter(ExtensionPayload),
            b'{"schema_id":"synthetic.empty","data":{}}',
            extensions=self.registry,
        )
        base = Path(os.environ.get("M1_04_DB_DIR", str(tmp_path)))
        base.mkdir(parents=True, exist_ok=True)
        self.db = SQLiteStore(
            base / (uuid4().hex + ".sqlite"), clock=lambda: self.time.now, extensions=self.registry
        )
        await self.db.upgrade(ROOT / "migrations")
        await self.db.provision(
            self.scope,
            (
                Grant(BEHAVIOR, Owner.BEHAVIOR, read=True, write=True),
                Grant(JOBS, Owner.JOBS, read=True, write=True),
                Grant(JOBS, Owner.BEHAVIOR, read=True),
            ),
        )
        self.store = SQLiteJobs(self.db, JOBS)
        self.policy = Policy(self.time, retry_safe=retry_safe)
        self.owner = JobOwner(
            self.store,
            self.supervisor,
            self.policy,
            clock=self.time.clock(),
            limits=limits,
            extensions=self.registry,
        )
        self.worker = WorkerAuthority(
            "synthetic-worker",
            frozenset({self.scope.partition}),
            frozenset({"synthetic.compute"}),
            3600,
        )
        return self

    async def command(self, operation="synthetic-operation"):
        token = self.supervisor.admit("activity")
        command = ExecutionCommand.model_validate(
            dict(
                id="command-" + operation,
                scope=self.scope,
                created_at=self.time.now,
                type="execution.command",
                operation_id=operation,
                target="synthetic-compute",
                binding_id="synthetic-binding",
                capability_id="synthetic.compute",
                capability_version=1,
                arguments=self.empty,
                permission_id="synthetic-permission",
                permission_version=1,
                deadline=self.time.now + timedelta(seconds=180),
                conditions=self.empty,
                cancellation_id=token.id,
                freshness=token.freshness,
            ),
            context={"extensions": self.registry},
        )
        incoming = ServiceClaimCommand(
            request_id="seed-" + operation,
            type="job.claim",
            scope=self.scope,
            target=operation,
            operation_id=operation,
            expected_version=1,
            freshness=token.freshness,
        )
        async with self.db.transaction(BEHAVIOR, self.scope, incoming) as unit:
            await unit.put(command, access=(RecordAccess(JOBS.id),))
            await unit.complete(
                Receipt(
                    request_id=incoming.request_id,
                    scope=self.scope,
                    status="completed",
                    resource_id=command.id,
                    version=1,
                )
            )
        return command, token

    async def queued(self, operation="synthetic-operation"):
        command, token = await self.command(operation)
        job = await self.owner.submit(command.id, self.scope, goal="Синтетическое вычисление")
        return job, command, token

    def claim_request(self, job, request_id=None):
        return ServiceClaimCommand(
            request_id=request_id or uuid4().hex,
            type="job.claim",
            scope=job.scope,
            target=job.operation_id,
            operation_id=job.operation_id,
            expected_version=job.version,
            freshness=job.freshness,
        )

    async def claimed(self, **kwargs):
        job, command, token = await self.queued(**kwargs)
        claim = await self.owner.claim(self.worker, self.claim_request(job))
        assert claim is not None
        return claim, command, token

    def result_request(self, claim, status="SUCCEEDED", *, request_id=None, result_id=None):
        result = ExecutionResult(
            id=result_id or "result-" + claim.lease.attempt_id,
            scope=claim.lease.scope,
            created_at=self.time.now,
            type="execution.result",
            command_id=claim.command.id,
            operation_id=claim.lease.operation_id,
            attempt_id=claim.lease.attempt_id,
            lease_version=claim.lease.lease_version,
            freshness=claim.lease.freshness,
            status=status,
            evidence=[],
            completed_at=self.time.now,
            external_ids=[],
            result=None,
        )
        return ServiceResultCommand(
            request_id=request_id or uuid4().hex,
            type="job.result",
            scope=result.scope,
            target=result.operation_id,
            result=result,
        )

    def stop(self):
        return self.supervisor.stop(
            self.operator,
            StopCommand(
                request_id=uuid4().hex,
                type="agent.stop",
                scope=self.scope,
                target="synthetic-agent",
            ),
        )


async def create(tmp_path, **kwargs):
    return await Fixture().setup(tmp_path, **kwargs)


def test_atomic_submission_replay_and_metadata_only_outbox(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, command, _ = await f.queued()
            replay = await f.owner.submit(command.id, f.scope, goal=job.goal)
            assert replay.id == job.id
            with pytest.raises(ContractError) as err:
                await f.owner.submit(command.id, f.scope, goal="Иная цель")
            assert err.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
            async with f.db.connection() as conn:
                assert await conn.scalar(select(func.count()).select_from(jobsdb.schedules)) == 1
                events = (
                    (await conn.execute(select(db.outbox).where(db.outbox.c.kind == "Job")))
                    .mappings()
                    .all()
                )
                assert len(events) == 1 and events[0]["status"] == "pending"
                assert "goal" not in events[0] and "arguments" not in events[0]
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_worker_cannot_write_or_self_authorize(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, _, _ = await f.queued()
            for worker in (
                replace(f.worker, partitions=frozenset()),
                replace(f.worker, capability_ids=frozenset()),
                replace(f.worker, expires_at=0),
            ):
                with pytest.raises(ContractError):
                    await f.owner.claim(worker, f.claim_request(job))
            with pytest.raises(ContractError):
                SQLiteJobs(f.db, Principal("worker", "worker"))
            f.policy.denied = True
            with pytest.raises(ContractError):
                await f.owner.claim(f.worker, f.claim_request(job))
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_claim_duplicate_delivery_same_worker_and_competing_worker(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, _, _ = await f.queued()
            request = f.claim_request(job)
            one = await f.owner.claim(f.worker, request)
            duplicate = await f.owner.claim(f.worker, request)
            assert one.lease == duplicate.lease
            assert one.acquired and not duplicate.acquired
            with pytest.raises(ContractError) as err:
                await f.owner.claim(replace(f.worker, id="other-worker"), request)
            assert err.value.error.code == ErrorCode.RESOURCE_BUSY
            async with f.db.connection() as conn:
                assert await conn.scalar(select(func.count()).select_from(jobsdb.attempts)) == 1
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_result_committed_before_ack_replay_never_creates_second_effect(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            request = f.result_request(claim)
            done = await f.owner.accept(f.worker, request)
            assert done.status == OperationState.SUCCEEDED
            duplicate = await f.owner.accept(f.worker, request)
            assert duplicate.version == done.version
            # Simulate crash after result commit and before XACK.
            restarted = JobOwner(f.store, f.supervisor, f.policy, clock=f.time.clock())
            assert await restarted.claim(f.worker, f.claim_request(done)) is None
            async with f.db.connection() as conn:
                assert (
                    await conn.scalar(
                        select(func.count())
                        .select_from(db.records)
                        .where(db.records.c.kind == "ExecutionResult")
                    )
                    == 1
                )
        finally:
            await f.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"])
def test_late_result_after_stop_is_technical_only(tmp_path, status):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, token = await f.claimed()
            f.stop()
            job = await f.owner.accept(f.worker, f.result_request(claim, status))
            expected = (
                OperationState.OUTCOME_UNKNOWN
                if status == "OUTCOME_UNKNOWN"
                else OperationState.CANCELLED
            )
            assert job.status == expected
            assert token.cancelled.is_set()
            assert f.supervisor.snapshot.state != RunState.ACTIVE
            with pytest.raises(ContractError):
                f.supervisor.resolve(token.id, scope=token.scope, freshness=token.freshness)
            assert await f.store.result("result-" + claim.lease.attempt_id, f.scope) is not None
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_bounded_safe_retry_and_attempt_ledger(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            first = await f.owner.accept(
                f.worker,
                f.result_request(claim, "FAILED"),
                transient=True,
                error=ErrorCode.SERVICE_UNAVAILABLE,
            )
            assert first.status == OperationState.WAITING
            with pytest.raises(ContractError):
                await f.owner.claim(f.worker, f.claim_request(first))
            f.time.advance(f.supervisor, 2)
            second = await f.owner.claim(f.worker, f.claim_request(first))
            assert second.lease.attempt_id != claim.lease.attempt_id
            assert second.lease.lease_version == 2
            done = await f.owner.accept(
                f.worker,
                f.result_request(second, "FAILED"),
                transient=True,
            )
            assert done.status == OperationState.FAILED
            assert await f.owner.claim(f.worker, f.claim_request(done)) is None
            async with f.db.connection() as conn:
                assert await conn.scalar(select(func.count()).select_from(jobsdb.attempts)) == 2
        finally:
            await f.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["FAILED", "OUTCOME_UNKNOWN"])
def test_unsafe_effect_never_automatically_retries(tmp_path, status):
    async def scenario():
        f = await create(tmp_path, retry_safe=False)
        try:
            claim, _, _ = await f.claimed()
            done = await f.owner.accept(f.worker, f.result_request(claim, status), transient=True)
            assert done.status == OperationState(status)
            assert await f.owner.claim(f.worker, f.claim_request(done)) is None
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_long_heartbeat_keeps_original_cancellation_id_and_attempt(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, command, token = await f.claimed()
            lease = claim.lease
            for _ in range(24):
                f.time.advance(f.supervisor, 4)
                lease = await f.owner.renew(
                    f.worker,
                    ServiceLeaseCommand(
                        request_id=uuid4().hex,
                        type="job.renew",
                        scope=f.scope,
                        target=lease.operation_id,
                        operation_id=lease.operation_id,
                        lease=lease,
                    ),
                )
                assert lease.attempt_id == claim.lease.attempt_id and lease.lease_version == 1
                current = f.supervisor.resolve(
                    command.cancellation_id,
                    scope=f.scope,
                    freshness=command.freshness,
                )
                assert current.id == token.id and current.cancelled is token.cancelled
            assert f.time.elapsed == 96
            done = await f.owner.accept(f.worker, f.result_request(claim))
            assert done.status == OperationState.SUCCEEDED
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_lease_reclaim_fences_old_worker_and_never_fakes_stop_receipt(tmp_path):
    async def scenario():
        f = await create(tmp_path, limits=JobLimits(lease_seconds=6))
        try:
            claim, _, _ = await f.claimed()
            f.time.advance(f.supervisor, 7)
            assert await f.owner.reconcile() == 1
            job, _ = await f.store.load(claim.lease.operation_id, f.scope)
            assert job.status == OperationState.WAITING
            assert f.supervisor.snapshot.unconfirmed_resource_ids
            f.time.advance(f.supervisor, 2)
            second = await f.owner.claim(f.worker, f.claim_request(job))
            assert second.lease.lease_version == 2
            with pytest.raises(ContractError):
                await f.owner.accept(f.worker, f.result_request(claim))
            done = await f.owner.accept(f.worker, f.result_request(second))
            assert done.status == OperationState.SUCCEEDED
            # Expired former executor was not magically acknowledged.
            assert f.supervisor.snapshot.unconfirmed_resource_ids
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_restart_closes_old_work_without_autorun(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            restarted_supervisor = Supervisor(
                BASE, "synthetic-agent", Journal(), Readiness(f.time), clock=f.time.clock()
            )
            restarted = JobOwner(f.store, restarted_supervisor, f.policy, clock=f.time.clock())
            assert await restarted.reconcile() == 1
            job, _ = await f.store.load(claim.lease.operation_id, f.scope)
            assert job.status == OperationState.OUTCOME_UNKNOWN
            assert await restarted.claim(f.worker, f.claim_request(job)) is None
            assert restarted_supervisor.snapshot.state == RunState.STOPPED
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_workspace_pending_quota_and_full_queue_stop(tmp_path):
    async def scenario():
        f = await create(tmp_path, limits=JobLimits(pending=1))
        try:
            await f.queued()
            command, _ = await f.command("synthetic-second")
            with pytest.raises(ContractError) as err:
                await f.owner.submit(command.id, f.scope, goal="Другая работа")
            assert err.value.error.code == ErrorCode.RESOURCE_BUSY
            assert f.stop().state == RunState.STOPPED
            assert await f.owner.reconcile() == 1
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_stop_during_policy_wait_prevents_claim(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, _, _ = await f.queued()
            f.policy.block = asyncio.Event()
            f.policy.entered.clear()
            pending = asyncio.create_task(f.owner.claim(f.worker, f.claim_request(job)))
            await f.policy.entered.wait()
            f.stop()
            f.policy.block.set()
            with pytest.raises(ContractError):
                await pending
            async with f.db.connection() as conn:
                assert await conn.scalar(select(func.count()).select_from(jobsdb.attempts)) == 0
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_empty_and_previous_schema_upgrade_single_head(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "previous.sqlite")
        try:
            await store.upgrade(ROOT / "migrations", revision="0002")
            await store.upgrade(ROOT / "migrations")
            await store.check_schema()
            async with store.connection() as conn:
                assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
                assert (await conn.execute(text("PRAGMA foreign_key_check"))).all() == []
                assert await conn.scalar(select(func.count()).select_from(jobsdb.schedules)) == 0
        finally:
            await store.close()

    asyncio.run(scenario())


class Broker:
    def __init__(self):
        self.items = []
        self.fail = False
        self.fail_after = False

    async def publish(self, reference):
        if self.fail:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        self.items.append(reference)
        if self.fail_after:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        return "1-0"


def test_broker_loss_and_publish_before_db_ack_reissues_same_operation(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, _, _ = await f.queued()
            broker = Broker()
            dispatcher = OutboxDispatcher(f.store, broker, clock=f.time.clock(), jitter=lambda: 1)
            broker.fail = True
            assert await dispatcher.dispatch() == 0
            broker.fail = False
            f.time.advance(f.supervisor, 5)
            broker.fail_after = True
            assert await dispatcher.dispatch() == 0
            broker.fail_after = False
            f.time.advance(f.supervisor, 5)
            assert await dispatcher.dispatch() == 1
            assert len(broker.items) == 2
            assert broker.items[0].event_id == broker.items[1].event_id
            # Redis has lost its delivery completely; DB redelivers even after delivered mark.
            broker.items.clear()
            f.time.advance(f.supervisor, 5)
            assert await dispatcher.dispatch() == 1
            assert broker.items[0].operation_id == job.operation_id
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_unknown_settlement_preserves_history_and_does_not_resume(tmp_path):
    async def scenario():
        f = await create(tmp_path, retry_safe=False)
        try:
            claim, _, _ = await f.claimed()
            unknown = f.result_request(claim, "OUTCOME_UNKNOWN")
            await f.owner.accept(f.worker, unknown)
            f.stop()
            confirmed = f.result_request(
                claim, "SUCCEEDED", result_id="synthetic-confirmed-receipt"
            )
            settled = await f.owner.settle_unknown(f.worker, confirmed)
            assert settled.status == OperationState.SUCCEEDED
            assert f.supervisor.snapshot.state == RunState.STOPPED
            assert (await f.store.result(unknown.result.id, f.scope)).status == "OUTCOME_UNKNOWN"
            assert (await f.owner.settle_unknown(f.worker, confirmed)).version == settled.version
            assert await f.owner.claim(f.worker, f.claim_request(settled)) is None
            async with f.db.connection() as conn:
                row = (await conn.execute(select(jobsdb.reconciliations))).mappings().one()
                metadata = json.loads(row["metadata_json"])
                assert metadata["status"] == "SUCCEEDED" and "result" not in metadata
        finally:
            await f.db.close()

    asyncio.run(scenario())


class FaultTransactionStore:
    def __init__(self, store, *, after=False, inside=False, block=False):
        self.store, self.after, self.inside = store, after, inside
        self.block = asyncio.Event() if block else None
        self.entered = asyncio.Event()

    def __getattr__(self, key):
        return getattr(self.store, key)

    @asynccontextmanager
    async def transaction(self, *args, **kwargs):
        async with self.store.transaction(*args, **kwargs) as tx:
            yield tx
            if self.inside:
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.after:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)


@pytest.mark.parametrize("after", [False, True])
def test_crash_before_or_after_commit_does_not_lose_or_double_submission(tmp_path, after):
    async def scenario():
        f = await create(tmp_path)
        try:
            command, _ = await f.command()
            f.owner.store = FaultTransactionStore(f.store, after=after, inside=not after)
            with pytest.raises(ContractError):
                await f.owner.submit(command.id, f.scope, goal="Синтетическая работа")
            stored = await f.store.load(command.operation_id, f.scope)
            assert bool(stored) is after
            f.owner.store = f.store
            saved = await f.owner.submit(command.id, f.scope, goal="Синтетическая работа")
            assert saved.operation_id == command.operation_id
            async with f.db.connection() as conn:
                assert await conn.scalar(select(func.count()).select_from(jobsdb.schedules)) == 1
        finally:
            await f.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("after", [False, True])
def test_crash_before_or_after_result_commit_returns_known_state(tmp_path, after):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            request = f.result_request(claim)
            f.owner.store = FaultTransactionStore(f.store, after=after, inside=not after)
            with pytest.raises(ContractError):
                await f.owner.accept(f.worker, request)
            f.owner.store = f.store
            done = await f.owner.accept(f.worker, request)
            assert done.status == OperationState.SUCCEEDED
            async with f.db.connection() as conn:
                assert (
                    await conn.scalar(
                        select(func.count())
                        .select_from(db.records)
                        .where(db.records.c.kind == "ExecutionResult")
                    )
                    == 1
                )
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_stop_after_lease_commit_before_return_does_not_issue_claim(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, _, token = await f.queued()
            fault = FaultTransactionStore(f.store, block=True)
            f.owner.store = fault
            task = asyncio.create_task(f.owner.claim(f.worker, f.claim_request(job)))
            await fault.entered.wait()
            f.stop()
            fault.block.set()
            with pytest.raises(ContractError):
                await task
            assert token.cancelled.is_set()
            f.owner.store = f.store
            assert await f.owner.reconcile() == 1
            unknown, _ = await f.store.load(job.operation_id, f.scope)
            assert unknown.status == OperationState.OUTCOME_UNKNOWN
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_real_locked_sqlite_writer_does_not_delay_direct_stop(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        lock = None
        try:
            job, _, _ = await f.queued()
            database = f.db.engine.url.database
            lock = sqlite3.connect(database, isolation_level=None)
            lock.execute("BEGIN IMMEDIATE")
            task = asyncio.create_task(f.owner.claim(f.worker, f.claim_request(job)))
            await asyncio.sleep(0.1)
            assert not task.done()
            started = time.perf_counter()
            f.stop()
            assert time.perf_counter() - started < 0.25
            lock.execute("ROLLBACK")
            with pytest.raises(ContractError):
                await task
            assert f.supervisor.snapshot.state == RunState.STOPPED
        finally:
            if lock:
                lock.close()
            await f.db.close()

    asyncio.run(scenario())


def test_expired_lease_and_rejected_final_policy_close_new_io(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            assert (await f.owner.verify(f.worker, claim.lease)).id == claim.command.id
            f.policy.denied = True
            with pytest.raises(ContractError):
                await f.owner.verify(f.worker, claim.lease)
            f.policy.denied = False
            f.time.advance(f.supervisor, 21)
            with pytest.raises(ContractError):
                await f.owner.verify(f.worker, claim.lease)
            with pytest.raises(ContractError):
                await f.owner.renew(
                    f.worker,
                    ServiceLeaseCommand(
                        request_id=uuid4().hex,
                        type="job.renew",
                        scope=f.scope,
                        target=claim.lease.operation_id,
                        operation_id=claim.lease.operation_id,
                        lease=claim.lease,
                    ),
                )
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_unknown_outcomes_hold_pending_quota_until_confirmed(tmp_path):
    async def scenario():
        f = await create(tmp_path, limits=JobLimits(pending=1))
        try:
            claim, _, _ = await f.claimed()
            await f.owner.accept(f.worker, f.result_request(claim, "OUTCOME_UNKNOWN"))
            command, _ = await f.command("synthetic-second")
            with pytest.raises(ContractError) as error:
                await f.owner.submit(command.id, f.scope, goal="Другая работа")
            assert error.value.error.code == ErrorCode.RESOURCE_BUSY
            await f.owner.settle_unknown(
                f.worker,
                f.result_request(
                    claim,
                    "CANCELLED",
                    result_id="confirmed-cancel",
                ),
            )
            assert (await f.owner.submit(command.id, f.scope, goal="Другая работа")).version == 1
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_real_cancel_receipt_of_old_attempt_leaves_new_result_intact(tmp_path):
    async def scenario():
        f = await create(tmp_path, limits=JobLimits(lease_seconds=6))
        try:
            old, _, _ = await f.claimed()
            f.time.advance(f.supervisor, 7)
            await f.owner.reconcile()
            job, _ = await f.store.load(old.lease.operation_id, f.scope)
            f.time.advance(f.supervisor, 2)
            new = await f.owner.claim(f.worker, f.claim_request(job))
            done = await f.owner.accept(f.worker, f.result_request(new))
            assert f.supervisor.snapshot.unconfirmed_resource_ids
            receipt = ServiceLeaseCommand(
                request_id=uuid4().hex,
                type="job.cancel_receipt",
                scope=f.scope,
                target=old.lease.operation_id,
                operation_id=old.lease.operation_id,
                lease=old.lease,
            )
            settled = await f.owner.cancel_receipt(f.worker, receipt)
            assert settled.status == done.status == OperationState.SUCCEEDED
            assert not f.supervisor.snapshot.unconfirmed_resource_ids
            assert (await f.owner.cancel_receipt(f.worker, receipt)).version == settled.version
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_reconciliation_pages_past_live_jobs_to_cancelled_tail(tmp_path):
    async def scenario():
        f = await create(tmp_path, limits=JobLimits(batch=2))
        try:
            tokens = []
            for suffix in ("a", "b", "c", "d"):
                _, _, token = await f.queued("operation-" + suffix)
                tokens.append(token)
            f.supervisor.retire(tokens[2])
            f.supervisor.retire(tokens[3])
            assert await f.owner.reconcile() == 0
            assert await f.owner.reconcile() == 2
            for suffix in ("c", "d"):
                job, _ = await f.store.load("operation-" + suffix, f.scope)
                assert job.status == OperationState.CANCELLED
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_renew_replay_and_changed_request_id_payload_conflict(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            f.time.advance(f.supervisor, 4)
            request = ServiceLeaseCommand(
                request_id="renew-exactly-once",
                type="job.renew",
                scope=f.scope,
                target=claim.lease.operation_id,
                operation_id=claim.lease.operation_id,
                lease=claim.lease,
            )
            one = await f.owner.renew(f.worker, request)
            duplicate = await f.owner.renew(f.worker, request)
            assert one == duplicate
            with pytest.raises(ContractError) as error:
                await f.owner.renew(f.worker, request.model_copy(update={"progress": 50}))
            assert error.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_changed_claim_body_cannot_reuse_same_request_id(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            job, _, _ = await f.queued()
            request = f.claim_request(job, "stable-claim")
            await f.owner.claim(f.worker, request)
            with pytest.raises(ContractError) as error:
                await f.owner.claim(
                    f.worker,
                    request.model_copy(update={"expected_version": job.version + 1}),
                )
            assert error.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
        finally:
            await f.db.close()

    asyncio.run(scenario())


def test_cancel_ack_does_not_rewrite_successful_attempt_history(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        try:
            claim, _, _ = await f.claimed()
            await f.owner.accept(f.worker, f.result_request(claim))
            request = ServiceLeaseCommand(
                request_id="actual-termination",
                type="job.cancel_receipt",
                scope=f.scope,
                target=claim.lease.operation_id,
                operation_id=claim.lease.operation_id,
                lease=claim.lease,
            )
            before = await f.db.repository(OperationProjection, JOBS, f.scope).get(
                claim.lease.operation_id
            )
            acknowledged = await f.owner.cancel_receipt(f.worker, request)
            after = await f.db.repository(OperationProjection, JOBS, f.scope).get(
                claim.lease.operation_id
            )
            assert before.result_id is not None and after.result_id == before.result_id
            assert acknowledged.status == OperationState.SUCCEEDED
            assert (
                await f.store.attempt(
                    claim.lease.operation_id,
                    claim.lease.attempt_id,
                    f.scope,
                )
            ).status == "succeeded"
            duplicate = await f.owner.cancel_receipt(f.worker, request)
            assert duplicate.version == acknowledged.version
        finally:
            await f.db.close()

    asyncio.run(scenario())
