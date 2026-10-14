"""Real SQLite + JobOwner + scheduler + mock gateway; no paid provider or listener."""

import asyncio

import pytest
from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.persona import CharacterState, ReflectionCheckpoint
from ai_agent_contracts.states import ErrorCode, OperationState, RunState
from composition_support import run, setup, stop
from dev_support import answer, idea

from ai_agent_core.m3_composition.schemas import TaskInput
from ai_agent_core.m3_composition.state import PRINCIPALS, put


def test_reflection_job_is_durable_bounded_and_replay_does_not_call_model(tmp_path):
    async def scenario():
        env = await setup(tmp_path)
        episode = await env.world.episode()
        env.world.provider.scripts.append(answer(changes=[idea(episodes=[episode])]))
        env.world.provider.scripts.append(answer(hard=[], soft=[], uncertain=False))
        rt = env.runtime
        request = TaskInput(kind="reflect", trigger_id="lived-session", episode_ids=[episode])
        job = await rt.submit(env.authority, "reflection-job", request)
        done = await rt.execute(job)
        assert done.status == OperationState.SUCCEEDED
        state = (
            await rt.store.repository(CharacterState, PRINCIPALS[Owner.PERSONA], job.scope).list()
        )[0]
        assert state.version == 2
        ledger = UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry)
        attempts = await ledger.attempts(job.scope)
        assert len(attempts) == 2 and all(a.status == "completed" for a in attempts)
        assert (await rt.submit(env.authority, "reflection-job", request)).id == job.id
        assert len(env.world.provider.calls) == 2
        await rt.close()
        rebuilt = env.build()
        await rebuilt.open()
        assert rebuilt.supervisor.snapshot.state == RunState.STOPPED
        checkpoint = await rt.store.repository(
            ReflectionCheckpoint, PRINCIPALS[Owner.PERSONA], job.scope
        ).list()
        assert len(checkpoint) == 1 and checkpoint[0].completed
        await env.activate(rebuilt)
        replay_job = await rebuilt.submit(env.authority, "after-restart", request)
        assert (await rebuilt.execute(replay_job)).status == OperationState.SUCCEEDED
        assert len(env.world.provider.calls) == 2
        await rebuilt.close()

    run(scenario())


@pytest.mark.parametrize("interrupt", ["stop", "permission"])
def test_stop_or_permission_revoke_before_model_completion_writes_no_change(tmp_path, interrupt):
    async def scenario():
        env = await setup(tmp_path)
        episode = await env.world.episode()
        entered, release = asyncio.Event(), asyncio.Event()

        class Provider:
            async def stream(self, call):
                entered.set()
                await release.wait()
                for event in answer(changes=[idea(episodes=[episode])]):
                    yield event

        rt = env.runtime
        rt.providers = lambda _: Provider()
        job = await rt.submit(
            env.authority,
            "blocked",
            TaskInput(kind="reflect", trigger_id="lived", episode_ids=[episode]),
        )
        task = asyncio.create_task(rt.execute(job))
        await asyncio.wait_for(entered.wait(), 3)
        if interrupt == "stop":
            assert stop(env).state in (RunState.STOPPING, RunState.STOPPED)
        else:
            old = await rt.store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], job.scope).get(
                "permit-reflect"
            )
            revoked = old.model_copy(
                update={"version": old.version + 1, "revoked_at": env.world.clock()}
            )
            await put(
                rt.store, rt.registry, Owner.WORKSPACE, old.scope, "revoke", revoked, old.version
            )
        release.set()
        finished = await asyncio.wait_for(task, 5)
        assert finished.status in (OperationState.CANCELLED, OperationState.FAILED)
        state = (
            await rt.store.repository(CharacterState, PRINCIPALS[Owner.PERSONA], job.scope).list()
        )[0]
        assert state.version == 1
        attempts = await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(
            job.scope
        )
        assert len(attempts) == 1 and attempts[0].status != "running"
        await rt.close()

    run(scenario())


def test_queued_jobs_are_closed_on_restart_and_different_scope_is_denied(tmp_path):
    async def scenario():
        env = await setup(tmp_path)
        episode = await env.world.episode()
        rt = env.runtime
        job = await rt.submit(
            env.authority,
            "queued",
            TaskInput(kind="reflect", trigger_id="lived", episode_ids=[episode]),
        )
        await rt.close()
        rebuilt = env.build()
        await rebuilt.open()
        stored = await rebuilt.jobs.store.load(job.operation_id, job.scope)
        assert stored[0].status == OperationState.CANCELLED
        assert (
            rebuilt.supervisor.snapshot.state == RunState.STOPPED and not env.world.provider.calls
        )
        foreign = env.authority.__class__("operator", frozenset(), 3600)
        with pytest.raises(ContractError):
            await rebuilt.submit(
                foreign,
                "denied",
                TaskInput(kind="reflect", trigger_id="lived", episode_ids=[episode]),
            )
        await rebuilt.close()

    run(scenario())


def test_current_permission_is_checked_inside_owner_commit(tmp_path):
    async def scenario():
        env = await setup(tmp_path)
        ep = await env.world.episode()
        rt = env.runtime
        job = await rt.submit(
            env.authority, "proof", TaskInput(kind="reflect", trigger_id="lived", episode_ids=[ep])
        )
        execution = await rt.jobs.store.command(job.command_id, job.scope)
        old = await rt.store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], job.scope).get(
            "permit-reflect"
        )
        await put(
            rt.store,
            rt.registry,
            Owner.WORKSPACE,
            old.scope,
            "revoke",
            old.model_copy(update={"version": old.version + 1, "revoked_at": env.world.clock()}),
            old.version,
        )
        from ai_agent_core.m3_composition.state import command

        with rt.store.authorize_writes(
            lambda connection: rt.policy.transaction_check(execution, connection),
            owners=frozenset({Owner.PERSONA}),
        ):
            with pytest.raises(ContractError) as failure:
                async with rt.store.transaction(
                    PRINCIPALS[Owner.PERSONA], job.scope, command(job.scope, "late-owner")
                ):
                    raise AssertionError("revoked owner transaction opened")
        assert failure.value.error.code in (ErrorCode.STALE_CONTEXT, ErrorCode.ACCESS_DENIED)
        await rt.close()

    run(scenario())


def test_recovery_applies_checkpointed_ready_change_once_without_new_model(tmp_path):
    async def scenario():
        from ai_agent_application.development import DevelopmentController

        env = await setup(tmp_path)
        episode = await env.world.episode()
        env.world.provider.scripts.extend(
            [
                answer(changes=[idea(episodes=[episode])]),
                answer(hard=[], soft=[], uncertain=False),
            ]
        )
        rt = env.runtime
        request = TaskInput(kind="reflect", trigger_id="unfinished-apply", episode_ids=[episode])
        original = DevelopmentController.apply

        async def fail_before_apply(*args, **kwargs):
            raise RuntimeError("synthetic crash after completed checkpoint")

        DevelopmentController.apply = fail_before_apply
        try:
            job = await rt.submit(env.authority, "before-restart", request)
            assert (await rt.execute(job)).status == OperationState.FAILED
        finally:
            DevelopmentController.apply = original
        await rt.close()
        rebuilt = env.build()
        await rebuilt.open()
        await env.activate(rebuilt)
        resumed = await rebuilt.submit(env.authority, "after-restart", request)
        assert (await rebuilt.execute(resumed)).status == OperationState.SUCCEEDED
        state = (
            await rt.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], resumed.scope
            ).list()
        )[0]
        assert state.version == 2 and len(env.world.provider.calls) == 2
        await rebuilt.close()

    run(scenario())
