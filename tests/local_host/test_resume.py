"""Opt-in resume after a restart: the journal's latest run decides; nothing else can outlive it."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ActivityState, ErrorCode, RunState

from ai_agent_core.local_host import resume as module
from ai_agent_core.local_host.resume import (
    Resume,
    ResumeError,
    ResumeSettings,
    authorizes,
    block_resume,
    read_block,
    resume_file,
)

BASE = Scope(
    workspace_id="w",
    persona_id="p",
    memory_space_id="m",
    memory_mode="work",
    agent_id="primary",
    run_id="idle-run",
)
NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
FAST = ResumeSettings(delay=0, interval=0, attempts=3)


def previous(**changes):
    values = dict(
        scope=BASE.model_copy(update={"run_id": "old-run"}),
        state=RunState.ACTIVE,
        autonomy="autonomous",
        activity=ActivityState.OPEN,
        created_at=NOW,
        ended_at=None,
        run_id="old-run",
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_a_run_the_process_left_running_on_her_own_authorizes_a_resume():
    assert authorizes(previous(), BASE, None)  # the process died while she ran
    assert authorizes(previous(state=RunState.STARTING), BASE, None)
    assert not authorizes(None, BASE, None)
    assert not authorizes(previous(state=RunState.FAULTED), BASE, None)  # a fault needs a hand
    assert not authorizes(previous(autonomy="observation"), BASE, None)
    for paused in (ActivityState.PAUSED, ActivityState.PAUSING):
        assert not authorizes(previous(activity=paused), BASE, None)


def test_a_stop_sets_ended_at_and_a_process_shutdown_does_not():
    shutdown = previous(state=RunState.STOPPED, ended_at=None)
    stopped = previous(state=RunState.STOPPED, ended_at=NOW)
    assert authorizes(shutdown, BASE, None)  # nobody ended that run: the process closed it
    assert not authorizes(stopped, BASE, None)  # an operator STOP always sets ended_at


def test_only_the_work_run_of_this_persona_may_be_resumed():
    test_base = BASE.model_copy(update={"memory_mode": "test"})
    assert not authorizes(previous(), test_base, None)
    assert not authorizes(previous(scope=test_base.model_copy(update={"run_id": "x"})), BASE, None)
    other = BASE.model_copy(update={"persona_id": "other", "run_id": "x"})
    assert not authorizes(previous(scope=other), BASE, None)


def test_a_restore_blocks_every_run_older_than_it():
    block = NOW + timedelta(minutes=1)
    assert not authorizes(previous(), BASE, block)  # older than the restore
    assert authorizes(previous(created_at=NOW + timedelta(minutes=2)), BASE, block)


def test_the_block_is_written_or_the_restore_does_not_begin(tmp_path, monkeypatch):
    block_resume(tmp_path)
    assert read_block(resume_file(tmp_path)) is not None

    def broken(*args, **kwargs):
        raise OSError("synthetic")

    monkeypatch.setattr(module.os, "replace", broken)
    with pytest.raises(ResumeError):
        block_resume(tmp_path)


def test_an_unreadable_or_broken_block_is_not_no_block(tmp_path, monkeypatch):
    path = resume_file(tmp_path)
    assert read_block(path) is None  # no file: no block
    path.parent.mkdir(parents=True)
    for text in ("{broken", "[]", '{"blocked_before": 5}', '{"blocked_before": "not a time"}'):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ResumeError):
            read_block(path)
    path.write_text(json.dumps({"blocked_before": NOW.isoformat()}), encoding="utf-8")
    assert read_block(path) == NOW

    def locked(self, *args, **kwargs):
        raise PermissionError("synthetic sharing violation")

    monkeypatch.setattr(Path, "read_text", locked)
    with pytest.raises(ResumeError):
        read_block(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; NTFS needs an ACL check")
def test_the_block_file_is_private_and_atomic(tmp_path):
    block_resume(tmp_path)
    path = resume_file(tmp_path)
    assert not path.with_suffix(".tmp").exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


class Supervisor:
    """The part of the Supervisor that Resume uses."""

    def __init__(self, last, refusals=()):
        self.previous = last
        self.snapshot = SimpleNamespace(
            state=RunState.STOPPED,
            autonomy="observation",
            activity=ActivityState.OPEN,
            version=3,
            run_id="idle-run",
            scope=BASE,
        )
        self.refusals, self.calls, self.observers = list(refusals), [], []
        self.clock = SimpleNamespace(elapsed=lambda: 100.0)

    def observe(self, callback):
        callback(self.snapshot, 0.0)  # like the real Supervisor: the current state at once
        self.observers.append(callback)
        return lambda: self.observers.remove(callback)

    async def control(self, authority, command):
        self.calls.append((authority, command))
        if self.refusals:
            raise ContractError(self.refusals.pop(0))

    def emit(self, **changes):
        self.snapshot = SimpleNamespace(**{**vars(self.snapshot), **changes})
        for callback in tuple(self.observers):
            callback(self.snapshot, 0.0)


def host(last, **kwargs):
    supervisor = Supervisor(last, **kwargs)
    return SimpleNamespace(supervisor=supervisor, running=True), supervisor


def test_after_a_crash_she_is_started_like_the_operator_would_start_her(tmp_path):
    async def scenario():
        runtime, supervisor = host(previous())
        await Resume(runtime, resume_file(tmp_path), FAST).run()
        [(authority, command)] = supervisor.calls
        assert command.type == "agent.start" and command.arguments.autonomy == "autonomous"
        assert command.expected_version == 3 and command.scope.memory_mode == "work"
        assert command.target == "primary" and command.request_id.startswith("resume-")
        assert authority.principal_id == "operator" and BASE.partition in authority.partitions

    asyncio.run(scenario())


def test_after_a_clean_shutdown_nobody_ended_that_run_so_she_is_started(tmp_path):
    async def scenario():
        runtime, supervisor = host(previous(state=RunState.STOPPED, ended_at=None))
        await Resume(runtime, resume_file(tmp_path), FAST).run()
        assert len(supervisor.calls) == 1

    asyncio.run(scenario())


def test_nothing_starts_when_the_latest_run_does_not_authorize(tmp_path):
    async def scenario():
        for last in (
            None,
            previous(state=RunState.STOPPED, ended_at=NOW),  # the operator's STOP
            previous(state=RunState.FAULTED),
            previous(autonomy="observation"),
            previous(activity=ActivityState.PAUSED),
        ):
            runtime, supervisor = host(last)
            await Resume(runtime, resume_file(tmp_path), FAST).run()
            assert supervisor.calls == []

    asyncio.run(scenario())


def test_an_unreadable_block_denies_the_resume_and_never_starts(tmp_path, monkeypatch):
    async def scenario():
        path = resume_file(tmp_path)
        block_resume(tmp_path)

        def locked(self, *args, **kwargs):
            raise PermissionError("synthetic sharing violation")

        monkeypatch.setattr(Path, "read_text", locked)
        runtime, supervisor = host(previous(created_at=NOW + timedelta(days=9)))
        await Resume(runtime, path, FAST).run()
        assert supervisor.calls == []

    asyncio.run(scenario())


def test_a_stop_or_a_manual_start_while_waiting_wins(tmp_path):
    async def scenario():
        runtime, supervisor = host(previous())
        slow = ResumeSettings(delay=0.05, interval=0, attempts=2)
        task = asyncio.create_task(Resume(runtime, resume_file(tmp_path), slow).run())
        await asyncio.sleep(0.01)
        supervisor.emit(state=RunState.STOPPING)  # the operator pressed STOP
        await task
        assert supervisor.calls == []

        runtime, supervisor = host(previous())
        task = asyncio.create_task(Resume(runtime, resume_file(tmp_path), slow).run())
        await asyncio.sleep(0.01)
        supervisor.snapshot = SimpleNamespace(**{**vars(supervisor.snapshot), "run_id": "new-run"})
        await task  # somebody started and stopped her meanwhile: another run
        assert supervisor.calls == []

    asyncio.run(scenario())


def test_it_waits_out_a_busy_host_but_gives_up_and_a_real_refusal_is_final(tmp_path):
    async def scenario():
        runtime, supervisor = host(previous(), refusals=[ErrorCode.RESOURCE_BUSY] * 2)
        await Resume(runtime, resume_file(tmp_path), FAST).run()
        assert len(supervisor.calls) == 3

        runtime, supervisor = host(previous(), refusals=[ErrorCode.RESOURCE_BUSY] * 9)
        await Resume(runtime, resume_file(tmp_path), FAST).run()
        assert len(supervisor.calls) == 3  # bounded: then START by hand

        runtime, supervisor = host(previous(), refusals=[ErrorCode.ACCESS_DENIED])
        await Resume(runtime, resume_file(tmp_path), FAST).run()
        assert len(supervisor.calls) == 1

    asyncio.run(scenario())


def test_close_does_no_filesystem_work(tmp_path, monkeypatch):
    async def scenario():
        runtime, _ = host(previous())
        resume = Resume(runtime, resume_file(tmp_path), FAST)
        resume.attach()

        def forbidden(*args, **kwargs):
            raise AssertionError("close() touched the disk")

        monkeypatch.setattr(Path, "write_text", forbidden)
        monkeypatch.setattr(module.os, "replace", forbidden)
        await resume.close()

    asyncio.run(scenario())
