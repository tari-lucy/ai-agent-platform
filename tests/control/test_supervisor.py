"""Synthetic protocol/real SQLite failure checks; no real models or integrations."""

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.control.journal import SQLiteRunJournal
from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import (
    Clock,
    ContextApproval,
    ControlAuthority,
    ControlLimits,
)
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.api import ControlArguments, ControlCommand, StopCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import RunSession
from ai_agent_contracts.states import ActivityState, ErrorCode, RunState
from sqlalchemy import func, select

from ai_agent_core.bootstrap import create_app

ROOT = Path(__file__).resolve().parents[2]
SCOPE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="synthetic-persona",
    memory_space_id="synthetic-space",
    memory_mode="test",
)
PRINCIPAL = Principal("synthetic-supervisor", "core_owner", Owner.SUPERVISOR)


class VirtualTime:
    def __init__(self):
        self.value = 0.0
        self.wall = datetime(2026, 10, 13, tzinfo=UTC)

    def elapsed(self):
        return self.value

    def utc(self):
        return self.wall

    def advance(self, seconds, *, elapsed=True):
        if elapsed:
            self.value += seconds
        self.wall += timedelta(seconds=seconds)

    def clock(self):
        return Clock(self.elapsed, self.utc)


class Journal:
    def __init__(self):
        self.records = {}
        self.block = None
        self.entered = asyncio.Event()
        self.block_state = None
        self.fail = False
        self.writes = 0

    async def load(self):
        if self.fail:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        return [
            item.model_copy(deep=True)
            for item in self.records.values()
            if item.state != RunState.STOPPED
        ]

    async def latest(self, scope):
        found = [item for item in self.records.values() if item.scope.partition == scope.partition]
        if not found:
            return None
        return max(found, key=lambda item: (item.updated_at, item.version)).model_copy(deep=True)

    async def save(self, snapshot):
        self.writes += 1
        if self.fail:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        if self.block is not None and (
            self.block_state is None or self.block_state == snapshot.state
        ):
            self.entered.set()
            await self.block.wait()
        self.records[snapshot.id] = snapshot.model_copy(deep=True)


class Readiness:
    def __init__(self, clock):
        self.clock = clock
        self.block = None
        self.entered = asyncio.Event()
        self.fail = False
        self.unknown = ()
        self.profile = 1
        self.policy = 1
        self.calls = 0
        self.wrong_scope = False

    async def check(self, run):
        self.calls += 1
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.fail:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        scope = run.scope
        if self.wrong_scope:
            scope = scope.model_copy(update={"persona_id": "other-persona"})
        return ContextApproval(scope, self.profile, self.policy, self.clock.elapsed() + 60)

    async def reconcile(self, previous):
        return self.unknown


def fixture(*, limits=None, journal=None, scope=SCOPE, gm_auto_start=False):
    clock = VirtualTime()
    journal = journal or Journal()
    readiness = Readiness(clock)
    owner = Supervisor(
        scope,
        "synthetic-agent",
        journal,
        readiness,
        clock=clock.clock(),
        limits=limits,
        gm_auto_start=gm_auto_start,
    )
    authority = ControlAuthority("synthetic-operator", frozenset({scope.partition}), 3600)
    return owner, authority, clock, journal, readiness


def command(owner, kind="agent.start", request_id="request-start", **changes):
    scope = owner.snapshot.scope
    values = dict(
        request_id=request_id,
        type=kind,
        scope=scope,
        target="synthetic-agent",
        arguments=ControlArguments(autonomy="autonomous"),
        expected_version=owner.snapshot.version,
    )
    values.update(changes)
    return ControlCommand(**values)


def stop_command(owner, request_id="request-stop", **changes):
    values = dict(
        request_id=request_id,
        type="agent.stop",
        scope=owner.snapshot.scope,
        target="synthetic-agent",
    )
    values.update(changes)
    return StopCommand(**values)


async def active(**kwargs):
    owner, authority, clock, journal, readiness = fixture(**kwargs)
    await owner.recover()
    await owner.control(authority, command(owner))
    return owner, authority, clock, journal, readiness


def rejected(code, call):
    with pytest.raises(ContractError) as error:
        call()
    assert error.value.error.code == code


@pytest.mark.parametrize(
    "configured,mode,autonomy,paused",
    [
        (False, "work", "autonomous", True),
        (True, "work", "autonomous", False),
        (True, "work", "observation", True),
        (True, "test", "autonomous", True),
    ],
)
def test_gm_auto_start_only_after_explicit_autonomous_work_start(
    configured, mode, autonomy, paused
):
    async def scenario():
        scope = SCOPE.model_copy(update={"memory_mode": mode})
        owner, authority, _, journal, _ = fixture(scope=scope, gm_auto_start=configured)
        assert owner.snapshot.state == RunState.STOPPED and owner.snapshot.gm_paused
        await owner.recover()
        assert owner.snapshot.state == RunState.STOPPED and owner.snapshot.gm_paused
        await owner.control(
            authority, command(owner, arguments=ControlArguments(autonomy=autonomy))
        )
        assert owner.snapshot.gm_paused is paused
        if not paused:
            admission = owner.admit("gm")
            owner.set_gm(authority, True, expected_version=owner.snapshot.version)
            with pytest.raises(ContractError):
                owner.validate(admission)
            owner.poll()
            assert owner.snapshot.gm_paused  # no tick silently removes a manual pause
        owner.stop(authority, stop_command(owner))
        with pytest.raises(ContractError):
            owner.admit("gm")
        if not paused:
            await owner.flush()
            await owner.control(authority, command(owner, request_id="new-start"))
            assert not owner.snapshot.gm_paused  # only a new explicit START resets the pause
        restarted, _, _, _, _ = fixture(scope=scope, gm_auto_start=configured, journal=journal)
        assert restarted.snapshot.state == RunState.STOPPED and restarted.snapshot.gm_paused

    asyncio.run(scenario())


def test_bootstrap_is_fresh_stopped_and_cannot_start_without_composition():
    async def scenario():
        settings = BootstrapSettings(workspace_id="synthetic-workspace")
        one = create_app(settings, previous_state="ACTIVE")
        two = create_app(settings, previous_state="FAULTED")
        owner = one.state.supervisor
        assert owner.snapshot.state == RunState.STOPPED
        assert owner.snapshot.run_id != two.state.supervisor.snapshot.run_id
        assert one.routes == []
        with pytest.raises(ContractError):
            await owner.recover()
        assert owner.snapshot.state == RunState.STOPPED
        async with one.router.lifespan_context(one):
            assert owner.snapshot.state == RunState.STOPPED
        assert owner.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_start_requires_explicit_recovery_and_current_version():
    async def scenario():
        owner, authority, *_ = fixture()
        with pytest.raises(ContractError) as error:
            await owner.control(authority, command(owner))
        assert error.value.error.code == ErrorCode.SERVICE_UNAVAILABLE
        await owner.recover()
        with pytest.raises(ContractError) as error:
            await owner.control(authority, command(owner, expected_version=2))
        assert error.value.error.code == ErrorCode.VERSION_CONFLICT
        await owner.control(authority, command(owner))
        assert owner.snapshot.state == RunState.ACTIVE

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["readiness", "starting", "active"])
def test_stop_during_start_never_reopens_admission(blocked):
    async def scenario():
        owner, authority, _, journal, readiness = fixture()
        await owner.recover()
        gate = asyncio.Event()
        if blocked == "readiness":
            readiness.block = gate
            entered = readiness.entered
        else:
            journal.block, journal.block_state = gate, RunState(blocked.upper())
            entered = journal.entered
        request = command(owner)
        starting = asyncio.create_task(owner.control(authority, request))
        await asyncio.wait_for(entered.wait(), 1)
        receipt = owner.stop(authority, stop_command(owner))
        assert receipt.state == RunState.STOPPED
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.admit("activity"))
        gate.set()
        with pytest.raises(ContractError) as error:
            await starting
        assert error.value.error.code == ErrorCode.STALE_CONTEXT
        assert owner.snapshot.state == RunState.STOPPED
        assert owner.persistence_pending
        journal.block = None
        await owner.flush()
        assert journal.records[owner.snapshot.id].state == RunState.STOPPED
        assert not owner.persistence_pending
        # Retrying accepted START after STOP does not start a second run.
        replay = await owner.control(authority, request)
        assert replay.status == "rejected"
        assert owner.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_cancelled_start_closes_admission():
    async def scenario():
        owner, authority, _, _, readiness = fixture()
        await owner.recover()
        readiness.block = asyncio.Event()
        task = asyncio.create_task(owner.control(authority, command(owner)))
        await readiness.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.snapshot.state == RunState.FAULTED
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.admit("activity"))

    asyncio.run(scenario())


@pytest.mark.parametrize("dependency", ["llm", "redis", "db"])
def test_stop_does_not_wait_for_dependency_and_late_result_cannot_apply(dependency):
    async def scenario():
        owner, authority, _, journal, _ = await active()
        admission = owner.admit("activity")
        ticket = owner.register(admission, "synthetic-" + dependency)
        gate = asyncio.Event()
        completed = []

        async def late_result():
            await gate.wait()
            return owner.finish(admission, lambda: completed.append("forbidden-late-effect"))

        task = asyncio.create_task(late_result())
        journal.fail = True
        before = time.perf_counter()
        receipt = owner.stop(authority, stop_command(owner))
        assert time.perf_counter() - before < 0.250
        assert receipt.state == RunState.STOPPING
        assert receipt.unconfirmed_resource_ids == [ticket.resource_id]
        assert admission.cancelled.is_set()
        gate.set()
        with pytest.raises(ContractError) as error:
            await task
        assert error.value.error.code == ErrorCode.STALE_CONTEXT
        assert completed == []
        owner.acknowledge(ticket)
        assert owner.snapshot.state == RunState.STOPPED
        with pytest.raises(ContractError):
            await owner.flush()
        assert owner.snapshot.state == RunState.STOPPED
        journal.fail = False
        await owner.flush()
        assert journal.records[owner.snapshot.id].state == RunState.STOPPED

    asyncio.run(scenario())


def test_unknown_cancel_faults_and_expiry_is_not_a_receipt():
    async def scenario():
        owner, authority, clock, _, _ = await active()
        admission = (
            owner.admit("voice") if owner.snapshot.voice_enabled else owner.admit("activity")
        )
        ticket = owner.register(admission, "unresponsive")
        request = stop_command(owner)
        old_epoch = owner.snapshot.control_epoch
        owner.stop(authority, request)
        clock.advance(2.1)
        assert owner.snapshot.state == RunState.STOPPING  # Docker Desktop needs seconds
        clock.advance(28.0)
        snapshot = owner.snapshot
        assert snapshot.state == RunState.FAULTED
        assert snapshot.unconfirmed_resource_ids == ["unresponsive"]
        assert snapshot.control_epoch > old_epoch
        receipt = owner.stop(authority, request)
        assert receipt.state == RunState.FAULTED
        # The late removal receipt settles the run: no second STOP is needed (Windows report).
        owner.acknowledge(ticket)
        assert owner.snapshot.state == RunState.STOPPED
        assert owner.snapshot.unconfirmed_resource_ids == []

    asyncio.run(scenario())


def test_pause_resume_rechecks_context_and_never_replays_old_admissions():
    async def scenario():
        owner, authority, _, _, readiness = await active()
        observation = owner.admit("observation")
        activity = owner.admit("activity")
        ticket = owner.register(activity, "activity-transition")
        paused = await owner.control(authority, command(owner, "agent.pause", "pause"))
        assert paused.status == "waiting"
        assert owner.snapshot.activity == ActivityState.PAUSING
        owner.validate(observation)
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(activity))
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.admit("reflection"))
        owner.acknowledge(ticket)
        assert owner.snapshot.activity == ActivityState.PAUSED
        await owner.control(authority, command(owner, "agent.resume", "resume"))
        assert readiness.calls == 2
        assert owner.snapshot.activity == ActivityState.OPEN
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(activity))
        owner.validate(owner.admit("activity"))

    asyncio.run(scenario())


def test_pause_deadline_revokes_and_exposes_unknown():
    async def scenario():
        owner, authority, clock, _, _ = await active()
        ticket = owner.register(owner.admit("activity"), "stuck-transition")
        await owner.control(authority, command(owner, "agent.pause", "pause"))
        clock.advance(2.1)
        assert owner.snapshot.state == RunState.FAULTED
        assert owner.snapshot.activity == ActivityState.PAUSED
        assert owner.last_error == ErrorCode.OUTCOME_UNKNOWN
        assert owner.snapshot.unconfirmed_resource_ids == [ticket.resource_id]

    asyncio.run(scenario())


@pytest.mark.parametrize("dimension", ["gm", "voice"])
def test_gm_and_voice_are_independent_and_cancelled_tokens_never_revive(dimension):
    async def scenario():
        owner, authority, _, _, _ = await active()
        owner.set_voice(authority, True, expected_version=owner.snapshot.version)
        owner.set_gm(authority, False, expected_version=owner.snapshot.version)
        behavior = owner.admit("activity")
        gm, voice = owner.admit("gm"), owner.admit("voice")
        if dimension == "gm":
            owner.set_gm(authority, True, expected_version=owner.snapshot.version)
            rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(gm))
            owner.validate(voice)
            owner.set_gm(authority, False, expected_version=owner.snapshot.version)
            rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(gm))
        else:
            owner.set_voice(authority, False, expected_version=owner.snapshot.version)
            rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(voice))
            owner.validate(gm)
            owner.set_voice(authority, True, expected_version=owner.snapshot.version)
            rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(voice))
        owner.validate(behavior)
        assert owner.snapshot.state == RunState.ACTIVE

    asyncio.run(scenario())


def test_voice_cannot_reenable_while_cancellation_unconfirmed():
    async def scenario():
        owner, authority, *_ = await active()
        owner.set_voice(authority, True, expected_version=owner.snapshot.version)
        ticket = owner.register(owner.admit("voice"), "voice-output")
        owner.set_voice(authority, False, expected_version=owner.snapshot.version)
        rejected(
            ErrorCode.RESOURCE_BUSY,
            lambda: owner.set_voice(
                authority,
                True,
                expected_version=owner.snapshot.version,
            ),
        )
        owner.acknowledge(ticket)
        owner.set_voice(authority, True, expected_version=owner.snapshot.version)

    asyncio.run(scenario())


@pytest.mark.parametrize("wall_only", [False, True])
def test_sleep_or_stalled_scheduler_closes_admission_and_requires_explicit_start(wall_only):
    async def scenario():
        owner, authority, clock, *_ = await active()
        admission = owner.admit("activity")
        clock.advance(30, elapsed=not wall_only)
        assert owner.snapshot.state == RunState.FAULTED
        assert admission.cancelled.is_set()
        assert owner.last_error == ErrorCode.STALE_CONTEXT
        clock.advance(1)
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(admission))
        owner.stop(authority, stop_command(owner))
        await owner.flush()
        await owner.control(authority, command(owner, request_id="explicit-start"))
        assert owner.snapshot.state == RunState.ACTIVE
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(admission))

    asyncio.run(scenario())


def test_scope_auth_and_full_run_are_checked_even_for_stop():
    async def scenario():
        owner, authority, clock, *_ = await active()
        for bad_scope in (
            owner.snapshot.scope.model_copy(update={"persona_id": "other"}),
            owner.snapshot.scope.model_copy(update={"memory_mode": "work"}),
        ):
            rejected(
                ErrorCode.ACCESS_DENIED,
                lambda bad_scope=bad_scope: owner.stop(
                    authority,
                    stop_command(owner, scope=bad_scope),
                ),
            )
        rejected(
            ErrorCode.STALE_CONTEXT,
            lambda: owner.stop(
                authority,
                stop_command(
                    owner,
                    scope=owner.snapshot.scope.model_copy(
                        update={"run_id": "old-run"},
                    ),
                ),
            ),
        )
        expired = replace(authority, expires_at=clock.elapsed())
        rejected(ErrorCode.ACCESS_DENIED, lambda: owner.stop(expired, stop_command(owner)))
        assert owner.snapshot.state == RunState.ACTIVE

    asyncio.run(scenario())


def test_idempotency_conflicts_and_old_stop_does_not_stop_new_run():
    async def scenario():
        owner, authority, _, journal, readiness = await active()
        original = stop_command(owner)
        receipt = owner.stop(authority, original)
        conflicting = original.model_copy(update={"target": "different-agent"})
        rejected(ErrorCode.ACCESS_DENIED, lambda: owner.stop(authority, conflicting))
        conflicting = original.model_copy(
            update={
                "scope": original.scope.model_copy(
                    update={"run_id": None, "session_id": None},
                )
            }
        )
        rejected(ErrorCode.IDEMPOTENCY_CONFLICT, lambda: owner.stop(authority, conflicting))
        await owner.flush()
        await owner.control(authority, command(owner, request_id="second-start"))
        calls = readiness.calls
        replay = owner.stop(authority, original)
        assert replay.run_id == receipt.run_id
        assert owner.snapshot.state == RunState.ACTIVE
        assert readiness.calls == calls
        assert len(journal.records) == 2

    asyncio.run(scenario())


def test_observation_mode_disallows_autonomous_work():
    async def scenario():
        owner, authority, *_ = fixture()
        await owner.recover()
        await owner.control(
            authority,
            command(
                owner,
                arguments=ControlArguments(autonomy="observation"),
            ),
        )
        owner.validate(owner.admit("observation"))
        for kind in ("activity", "voice", "gm", "reflection"):
            rejected(ErrorCode.STALE_CONTEXT, lambda kind=kind: owner.admit(kind))

    asyncio.run(scenario())


def test_wrong_readiness_scope_or_failed_health_never_activates():
    async def scenario():
        for wrong_scope in (False, True):
            owner, authority, _, _, readiness = fixture()
            await owner.recover()
            readiness.wrong_scope, readiness.fail = wrong_scope, not wrong_scope
            with pytest.raises(ContractError):
                await owner.control(authority, command(owner))
            assert owner.snapshot.state == RunState.FAULTED
            rejected(ErrorCode.STALE_CONTEXT, lambda owner=owner: owner.admit("activity"))

    asyncio.run(scenario())


def test_bounded_buffers_keep_stop_available():
    async def scenario():
        limits = ControlLimits(resources=1, admissions=2, requests=1)
        owner, authority, clock, *_ = await active(limits=limits)
        one, two = owner.admit("activity"), owner.admit("observation")
        rejected(ErrorCode.RESOURCE_BUSY, lambda: owner.admit("activity"))
        owner.register(one, "one-resource")
        rejected(ErrorCode.RESOURCE_BUSY, lambda: owner.register(two, "two-resource"))
        assert owner.stop(authority, stop_command(owner)).state == RunState.STOPPING
        clock.advance(30.1)  # past the 30 s STOP receipt wait
        assert owner.snapshot.state == RunState.FAULTED

    asyncio.run(scenario())


def test_lease_expiry_and_forged_or_other_owner_admission_rejected():
    async def scenario():
        owner, _, clock, *_ = await active()
        admission = owner.admit("activity", seconds=1)
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(replace(admission)))
        other, *_ = await active()
        rejected(ErrorCode.STALE_CONTEXT, lambda: other.validate(admission))
        clock.advance(1)
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(admission))
        assert admission.cancelled.is_set()

    asyncio.run(scenario())


def test_final_handoff_rejects_async_work_and_after_stop():
    async def scenario():
        owner, authority, *_ = await active()
        admission = owner.admit("activity")
        applied = []
        owner.finish(admission, lambda: applied.append("before-stop"))

        async def forbidden_async():
            applied.append("async-effect")

        rejected(ErrorCode.INVALID_ARGUMENTS, lambda: owner.finish(admission, forbidden_async))
        owner.stop(authority, stop_command(owner))
        rejected(
            ErrorCode.STALE_CONTEXT,
            lambda: owner.finish(
                admission,
                lambda: applied.append("after-stop"),
            ),
        )
        assert applied == ["before-stop"]

    asyncio.run(scenario())


def test_other_thread_cannot_bypass_single_state_owner():
    owner, *_ = fixture()
    errors = []

    def touch():
        try:
            owner.shutdown()
        except RuntimeError as error:
            errors.append(str(error))

    thread = Thread(target=touch)
    thread.start()
    thread.join()
    assert errors == ["CONTROL_OWNER_THREAD_REQUIRED"]


async def real_fixture(path, scopes=(SCOPE,)):
    store = SQLiteStore(path)
    await store.upgrade(ROOT / "migrations")
    for scope in scopes:
        await store.provision(scope, (Grant(PRINCIPAL, Owner.SUPERVISOR, read=True, write=True),))
    journal = SQLiteRunJournal(store, PRINCIPAL, scopes)
    owner, authority, clock, _, readiness = fixture(journal=journal)
    return store, owner, authority, clock, journal, readiness


def test_real_sqlite_atomic_journal_and_restart_without_autorun(sqlite_dir):
    async def scenario():
        store, owner, authority, clock, journal, readiness = await real_fixture(
            sqlite_dir / "run.db"
        )
        try:
            await owner.recover()
            await owner.control(authority, command(owner))
            admission = owner.admit("activity")
            previous = (await journal.load())[0]
            assert previous.state == RunState.ACTIVE
            # Persisted ACTIVE never becomes a fresh boot admission.
            restarted = Supervisor(
                SCOPE, "synthetic-agent", journal, readiness, clock=clock.clock()
            )
            assert restarted.snapshot.state == RunState.STOPPED
            assert restarted.snapshot.run_id != admission.freshness.run_id
            rejected(ErrorCode.STALE_CONTEXT, lambda: restarted.validate(admission))
            await restarted.recover()
            assert await journal.load() == []
            assert restarted.snapshot.state == RunState.STOPPED
            record = await store.repository(RunSession, PRINCIPAL, previous.scope).get(previous.id)
            assert record.state == RunState.STOPPED
            assert record.version == 3
            async with store.connection() as connection:
                for table in (db.revisions, db.operations, db.outbox):
                    assert await connection.scalar(select(func.count()).select_from(table)) == 3
        finally:
            await store.close()

    asyncio.run(scenario())


def test_real_workspace_slot_rejects_second_persona_until_reconciliation(sqlite_dir):
    async def scenario():
        other = SCOPE.model_copy(
            update={
                "persona_id": "other-persona",
                "memory_space_id": "work-space",
                "memory_mode": "work",
            }
        )
        store, owner, authority, clock, journal, readiness = await real_fixture(
            sqlite_dir / "run.db",
            (SCOPE, other),
        )
        try:
            await owner.recover()
            await owner.control(authority, command(owner))
            second, auth_two, _, _, check_two = fixture(journal=journal, scope=other)
            # Do not reconcile a live owner's state: simulate accepted bootstrap before first start.
            await second.recover()  # fixture readiness confirms the old synthetic run stopped
            # First owner's memory must also close; real composition has exactly one supervisor.
            owner.shutdown()
            await second.control(auth_two, command(second))
            readiness.unknown = ("unconfirmed-executor",)
            restarted = Supervisor(
                SCOPE, "synthetic-agent", journal, readiness, clock=clock.clock()
            )
            with pytest.raises(ContractError) as error:
                await restarted.recover()
            assert error.value.error.code == ErrorCode.OUTCOME_UNKNOWN
            assert restarted.snapshot.state == RunState.STOPPED
            assert (await journal.load())[0].state == RunState.FAULTED
            with pytest.raises(ContractError):
                await restarted.control(authority, command(restarted))
            readiness.unknown = ()
            await restarted.recover()
            await restarted.control(authority, command(restarted))
            assert restarted.snapshot.state == RunState.ACTIVE
        finally:
            await store.close()

    asyncio.run(scenario())


def test_real_sqlite_busy_writer_does_not_delay_stop_or_commit_late_action(sqlite_dir):
    async def scenario():
        path = sqlite_dir / "blocked.db"
        store, owner, authority, _, journal, _ = await real_fixture(path)
        blocker = None
        try:
            await owner.recover()
            await owner.control(authority, command(owner))
            admission = owner.admit("activity")
            blocker = sqlite3.connect(path, isolation_level=None)
            blocker.execute("BEGIN IMMEDIATE")
            save = asyncio.create_task(owner.flush())
            # A real writer is blocked by another connection; direct path must still run.
            await asyncio.sleep(0.05)
            assert not save.done()
            before = time.perf_counter()
            receipt = owner.stop(authority, stop_command(owner))
            assert time.perf_counter() - before < 0.250
            assert receipt.state == RunState.STOPPED
            applied = []
            rejected(
                ErrorCode.STALE_CONTEXT,
                lambda: owner.finish(
                    admission,
                    lambda: applied.append("forbidden"),
                ),
            )
            blocker.execute("ROLLBACK")
            blocker.close()
            blocker = None
            await save
            assert owner.persistence_pending
            await owner.flush()
            assert await journal.load() == []
            assert applied == []
        finally:
            if blocker:
                blocker.close()
            await store.close()

    asyncio.run(scenario())


def test_sqlite_unknown_profile_occupancy_cannot_be_ignored(sqlite_dir):
    async def scenario():
        other = SCOPE.model_copy(update={"persona_id": "other-persona"})
        store, owner, authority, _, _, _ = await real_fixture(sqlite_dir / "run.db", (SCOPE, other))
        try:
            await owner.recover()
            await owner.control(authority, command(owner))
            restricted = SQLiteRunJournal(store, PRINCIPAL, (other,))
            with pytest.raises(ContractError) as error:
                await restricted.load()
            assert error.value.error.code == ErrorCode.ACCESS_DENIED
        finally:
            await store.close()

    asyncio.run(scenario())


def test_revocation_latency_distribution_at_full_capacity(tmp_path):
    async def scenario():
        measurements = []
        for _cycle in range(40):
            owner, authority, *_ = await active()
            for index in range(200):
                owner.register(owner.admit("activity"), f"resource-{index}")
            for _ in range(56):
                owner.admit("activity")
            before = time.perf_counter()
            receipt = owner.stop(authority, stop_command(owner))
            measurements.append((time.perf_counter() - before) * 1000)
            assert len(receipt.unconfirmed_resource_ids) == 200
        values = sorted(measurements)
        result = {
            "scenario": "synthetic-200-resources-256-admissions",
            "samples": len(values),
            "p95_ms": values[37],
            "p99_ms": values[39],
        }
        (tmp_path / "stop-latency.json").write_text(json.dumps(result), encoding="utf-8")
        print("STOP_LATENCY " + json.dumps(result))
        assert result["p95_ms"] <= 250
        assert result["p99_ms"] <= 500

    asyncio.run(scenario())


@pytest.fixture
def sqlite_dir(tmp_path):
    configured = os.environ.get("M1_03_DB_DIR")
    if not configured:
        return tmp_path
    directory = Path(configured) / ("synthetic-control-" + uuid4().hex)
    directory.mkdir(parents=True)
    return directory


def test_real_second_persona_reservation_conflicts_without_recovery_of_live_owner(sqlite_dir):
    async def scenario():
        other = SCOPE.model_copy(update={"persona_id": "other-persona"})
        store, owner, authority, _, journal, _ = await real_fixture(
            sqlite_dir / "exclusive.db",
            (SCOPE, other),
        )
        try:
            second, auth_two, *_ = fixture(journal=journal, scope=other)
            await owner.recover()
            await second.recover()
            await owner.control(authority, command(owner))
            with pytest.raises(ContractError) as error:
                await second.control(auth_two, command(second))
            assert error.value.error.code == ErrorCode.VERSION_CONFLICT
            assert second.snapshot.state == RunState.FAULTED
            assert owner.snapshot.state == RunState.ACTIVE
            assert (await journal.load())[0].scope.partition == SCOPE.partition
        finally:
            await store.close()

    asyncio.run(scenario())


def test_resume_rejects_old_profile_policy_even_for_observation():
    async def scenario():
        owner, authority, _, _, readiness = await active()
        old = owner.admit("observation")
        await owner.control(authority, command(owner, "agent.pause", "pause"))
        readiness.profile, readiness.policy = 2, 3
        await owner.control(authority, command(owner, "agent.resume", "resume"))
        rejected(ErrorCode.STALE_CONTEXT, lambda: owner.validate(old))
        new = owner.admit("observation")
        assert new.freshness.profile_version == 2
        assert new.freshness.policy_version == 3

    asyncio.run(scenario())


def test_expired_session_during_start_never_activates():
    async def scenario():
        owner, authority, clock, _, readiness = fixture()
        authority = replace(authority, expires_at=0.5)
        await owner.recover()
        readiness.block = asyncio.Event()
        task = asyncio.create_task(owner.control(authority, command(owner)))
        await readiness.entered.wait()
        clock.advance(1)
        readiness.block.set()
        with pytest.raises(ContractError) as error:
            await task
        assert error.value.error.code == ErrorCode.ACCESS_DENIED
        assert owner.snapshot.state == RunState.FAULTED

    asyncio.run(scenario())


def test_watchdog_settles_timeout_without_ui_poll():
    async def scenario():
        owner, authority, clock, *_ = await active()
        owner.register(owner.admit("activity"), "stuck")
        owner.stop(authority, stop_command(owner))
        watchdog = asyncio.create_task(owner.watchdog())
        await asyncio.sleep(0.01)
        clock.advance(30.1)  # past the 30 s STOP receipt wait
        await asyncio.sleep(0.08)
        assert owner._run.state == RunState.FAULTED
        watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog

    asyncio.run(scenario())


def test_observation_resource_does_not_block_activity_resume():
    async def scenario():
        owner, authority, *_ = await active()
        observation = owner.admit("observation")
        ticket = owner.register(observation, "ongoing-observation")
        await owner.control(authority, command(owner, "agent.pause", "pause"))
        assert owner.snapshot.activity == ActivityState.PAUSED
        await owner.control(authority, command(owner, "agent.resume", "resume"))
        owner.validate(observation)
        owner.acknowledge(ticket)
        assert owner.snapshot.state == RunState.ACTIVE

    asyncio.run(scenario())


def test_invalid_resource_id_does_not_mutate_registry():
    async def scenario():
        owner, authority, *_ = await active()
        rejected(
            ErrorCode.INVALID_ARGUMENTS,
            lambda: owner.register(
                owner.admit("activity"),
                "invalid resource id",
            ),
        )
        assert owner.stop(authority, stop_command(owner)).state == RunState.STOPPED

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 0.0])
def test_nonfinite_or_nonpositive_limits_rejected(value):
    with pytest.raises(ValueError, match="CONTROL_LIMIT"):
        ControlLimits(lease_seconds=value)


def test_nonfinite_authority_expiry_rejected():
    with pytest.raises(ValueError, match="CONTROL_AUTHORITY_EXPIRY"):
        ControlAuthority("synthetic-operator", frozenset({SCOPE.partition}), float("nan"))


def test_wire_cancellation_id_cannot_be_reissued_after_pause_or_stop():
    async def scenario():
        owner, authority, *_ = await active()
        old = owner.admit("activity")
        assert owner.resolve(old.id, scope=old.scope, freshness=old.freshness) is old
        await owner.control(authority, command(owner, "agent.pause", "pause"))
        await owner.control(authority, command(owner, "agent.resume", "resume"))
        rejected(
            ErrorCode.STALE_CONTEXT,
            lambda: owner.resolve(
                old.id,
                scope=old.scope,
                freshness=old.freshness,
            ),
        )
        new = owner.admit("activity")
        # An epoch is global; pause revokes individual cancellation IDs independently.
        assert old.freshness == new.freshness
        assert old.id != new.id
        rejected(
            ErrorCode.STALE_CONTEXT,
            lambda: owner.resolve(
                new.id,
                scope=new.scope.model_copy(update={"memory_mode": "work"}),
                freshness=new.freshness,
            ),
        )
        owner.stop(authority, stop_command(owner))
        rejected(
            ErrorCode.STALE_CONTEXT,
            lambda: owner.resolve(
                new.id,
                scope=new.scope,
                freshness=new.freshness,
            ),
        )

    asyncio.run(scenario())


def test_refresh_preserves_cancellation_and_extends_only_live_admission():
    async def scenario():
        owner, authority, clock, _, _ = await active()
        token = owner.admit("activity")
        ticket = owner.register(token, "synthetic-long-job")
        for _ in range(15):
            clock.advance(4)
            owner.poll()
        # Polling alone did not extend the original 20 second admission.
        with pytest.raises(ContractError):
            await owner.refresh(token)
        owner.acknowledge(ticket)

    asyncio.run(scenario())


def test_heartbeat_extends_original_id_beyond_readiness_expiry():
    async def scenario():
        owner, authority, clock, _, _ = await active()
        token = owner.admit("activity")
        original_id, event = token.id, token.cancelled
        for _ in range(24):
            clock.advance(4)
            token = await owner.refresh(token)
            assert token.id == original_id and token.cancelled is event
            owner.validate(token)
        owner.stop(authority, stop_command(owner))
        assert event.is_set()
        with pytest.raises(ContractError):
            await owner.refresh(token)

    asyncio.run(scenario())


def test_stop_while_refresh_waits_cannot_reopen_admission():
    async def scenario():
        owner, authority, clock, _, readiness = await active()
        token = owner.admit("activity")
        readiness.block = asyncio.Event()
        readiness.entered.clear()
        pending = asyncio.create_task(owner.refresh(token))
        await readiness.entered.wait()
        owner.stop(authority, stop_command(owner))
        readiness.block.set()
        with pytest.raises(ContractError):
            await pending
        assert token.cancelled.is_set() and owner.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())
