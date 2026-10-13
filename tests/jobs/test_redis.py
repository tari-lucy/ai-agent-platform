"""Real Redis only on an explicitly supplied isolated test network; never use local env."""

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from ai_agent_adapters.broker.streams import RedisStreams
from ai_agent_application.jobs.owner import OutboxDispatcher
from ai_agent_application.jobs.ports import DispatchRef
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, OperationState, RunState
from redis.asyncio import Redis
from test_jobs import BASE, create

pytestmark = pytest.mark.skipif(
    not os.environ.get("M1_04_REDIS_HOST"),
    reason="isolated Redis host not supplied",
)


def client():
    return Redis(
        host=os.environ["M1_04_REDIS_HOST"],
        password="synthetic-test-only",
        socket_timeout=2,
        socket_connect_timeout=2,
        max_connections=4,
        decode_responses=True,
        protocol=2,
    )


def reference():
    return DispatchRef(uuid4().hex, "synthetic-operation", "synthetic-job", 1, BASE)


def test_real_stream_capacity_pending_reclaim_and_ack():
    async def scenario():
        redis = client()
        broker = RedisStreams(
            redis, stream="synthetic-" + uuid4().hex, group="synthetic-workers", capacity=2
        )
        try:
            await broker.prepare()
            first = await broker.publish(reference())
            await broker.publish(reference())
            pending = await broker.read("worker-one")
            assert pending[0].id == first
            with pytest.raises(ContractError) as error:
                await broker.publish(reference())
            assert error.value.error.code == ErrorCode.RESOURCE_BUSY
            assert await redis.xlen(broker.stream) == 2
            await asyncio.sleep(0.03)
            reclaimed = await broker.reclaim("worker-two", idle_ms=1)
            assert reclaimed[0].id == first
            await broker.acknowledge(first)
            await broker.acknowledge(first)
            assert await redis.xlen(broker.stream) == 1
            assert (await redis.xpending(broker.stream, broker.group))["pending"] == 0
            await broker.publish(reference())
            assert await redis.xlen(broker.stream) == 2
        finally:
            await redis.aclose()

    asyncio.run(scenario())


def test_real_publish_claim_result_commit_lost_ack_redelivery(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        redis = client()
        broker = RedisStreams(redis, stream="synthetic-" + uuid4().hex, group="synthetic-workers")
        try:
            await broker.prepare()
            job, _, _ = await f.queued()
            dispatcher = OutboxDispatcher(f.store, broker, clock=f.time.clock(), jitter=lambda: 1)
            assert await dispatcher.dispatch() == 1
            one = (await broker.read("worker-one"))[0]
            claim = await f.owner.claim(f.worker, f.claim_request(job))
            done = await f.owner.accept(f.worker, f.result_request(claim))
            assert done.status == OperationState.SUCCEEDED
            # Crash after durable completion but before ACK. Redis reclaim grants no new attempt.
            await asyncio.sleep(0.03)
            redelivery = (await broker.reclaim("worker-two", idle_ms=1))[0]
            assert redelivery.reference.operation_id == one.reference.operation_id
            assert (
                await f.owner.claim(
                    replace(f.worker, id="worker-two"),
                    f.claim_request(done),
                )
                is None
            )
            await broker.acknowledge(redelivery.id)
            assert await redis.xlen(broker.stream) == 0
        finally:
            await redis.aclose()
            await f.db.close()

    asyncio.run(scenario())


def test_real_broker_delivery_loss_rebuilds_from_sqlite(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        redis = client()
        broker = RedisStreams(redis, stream="synthetic-" + uuid4().hex, group="synthetic-workers")
        try:
            await broker.prepare()
            job, _, _ = await f.queued()
            dispatcher = OutboxDispatcher(f.store, broker, clock=f.time.clock(), jitter=lambda: 1)
            assert await dispatcher.dispatch() == 1
            # Delete ONLY this disposable fixture stream, simulating lost AOF delivery.
            await redis.delete(broker.stream)
            await broker.prepare()
            f.time.advance(f.supervisor, 5)
            assert await dispatcher.dispatch() == 1
            reference = (await broker.read("worker-recovery"))[0]
            assert reference.reference.operation_id == job.operation_id
            assert "arguments" not in (await redis.xrange(broker.stream))[0][1]
        finally:
            await redis.aclose()
            await f.db.close()

    asyncio.run(scenario())


def test_real_full_or_paused_broker_does_not_delay_stop(tmp_path):
    async def scenario():
        f = await create(tmp_path)
        redis = client()
        broker = RedisStreams(
            redis,
            stream="synthetic-" + uuid4().hex,
            group="synthetic-workers",
            capacity=1,
            timeout_seconds=0.2,
        )
        try:
            await broker.prepare()
            await broker.publish(reference())
            await f.queued()
            dispatcher = OutboxDispatcher(f.store, broker, clock=f.time.clock(), jitter=lambda: 1)
            assert await dispatcher.dispatch() == 0
            f.time.advance(f.supervisor, 5)
            await redis.execute_command("CLIENT", "PAUSE", 400, "ALL")
            pending = asyncio.create_task(dispatcher.dispatch())
            await asyncio.sleep(0.03)
            started = asyncio.get_running_loop().time()
            f.stop()
            assert asyncio.get_running_loop().time() - started < 0.25
            assert await pending == 0
            assert f.supervisor.snapshot.state == RunState.STOPPED
        finally:
            await redis.aclose()
            await f.db.close()

    asyncio.run(scenario())
