"""Opt-in resume after a restart: the journal decides, the marker only adds a clean shutdown."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
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
    mark_clean,
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
        run_id="old-run",
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_only_a_run_the_process_left_running_on_her_own_authorizes_a_resume():
    assert authorizes(previous(), BASE, {})  # the process died while she ran
    assert authorizes(previous(state=RunState.STARTING), BASE, {})
    assert not authorizes(None, BASE, {})
    assert not authorizes(previous(state=RunState.FAULTED), BASE, {})  # a fault needs a hand
    assert not authorizes(previous(autonomy="observation"), BASE, {})
    for paused in (ActivityState.PAUSED, ActivityState.PAUSING):
        assert not authorizes(previous(activity=paused), BASE, {})


def clean(**changes):
    values = {
        "clean_run_id": "old-run",
        "clean_partition": list(BASE.partition),
        "clean_at": NOW.isoformat(),
    }
    values.update(changes)
    return values


def test_an_operator_stop_leaves_nothing_in_the_journal_and_never_authorizes():
    assert not authorizes(None, BASE, {})  # a stopped run frees its slot: nothing to resume
    assert not authorizes(previous(state=RunState.STOPPED), BASE, {})


def test_a_clean_shutdown_marker_authorizes_only_for_this_persona_and_when_valid():
    assert authorizes(None, BASE, clean())
    assert not authorizes(None, BASE, clean(clean_partition=["w", "other", "m", "work"]))
    assert not authorizes(None, BASE, clean(clean_run_id=None))
    assert not authorizes(None, BASE, clean(clean_at="not a time"))
    assert not authorizes(None, BASE, clean(clean_at=None))
    test_base = BASE.model_copy(update={"memory_mode": "test"})
    assert not authorizes(None, test_base, clean())
    # An unfinished fault or pause in the journal overrules a marker next to it.
    assert not authorizes(previous(state=RunState.FAULTED), BASE, clean())
    assert not authorizes(previous(activity=ActivityState.PAUSED), BASE, clean())


def test_only_the_work_run_of_this_persona_may_be_resumed():
    test_base = BASE.model_copy(update={"memory_mode": "test"})
    assert not authorizes(previous(), test_base, {})
    assert not authorizes(previous(scope=test_base.model_copy(update={"run_id": "x"})), BASE, {})
    other = BASE.model_copy(update={"persona_id": "other", "run_id": "x"})
    assert not authorizes(previous(scope=other), BASE, {})


def test_a_restore_blocks_every_run_older_than_it():
    block = {"blocked_before": (NOW + timedelta(minutes=1)).isoformat()}
    assert not authorizes(previous(), BASE, block)  # the run is older than the restore
    assert authorizes(previous(created_at=NOW + timedelta(minutes=2)), BASE, block)
    assert not authorizes(None, BASE, {**block, **clean()})  # a marker older than the restore
    later = clean(clean_at=(NOW + timedelta(minutes=2)).isoformat())
    assert authorizes(None, BASE, {**block, **later})
    assert not authorizes(previous(), BASE, {"blocked_before": "not a time"})
    assert not authorizes(previous(), BASE, {"blocked_before": 5})


def test_block_resume_writes_the_block_or_refuses(tmp_path, monkeypatch):
    block_resume(tmp_path)
    data = json.loads(resume_file(tmp_path).read_text(encoding="utf-8"))
    assert data["blocked_before"] and data["clean_run_id"] is None

    def broken(path, data):
        raise OSError("synthetic")

    monkeypatch.setattr(module, "_store", broken)
    with pytest.raises(ResumeError):
        block_resume(tmp_path)  # the restore must not begin


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; NTFS needs an ACL check")
def test_the_marker_is_private_and_atomic(tmp_path):
    path = resume_file(tmp_path)
    mark_clean(path, "run-1", BASE.partition)
    assert json.loads(path.read_text(encoding="utf-8"))["clean_run_id"] == "run-1"
    assert not path.with_suffix(".tmp").exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


class Supervisor:
    """The part of the Supervisor that Resume uses."""

    def __init__(self, last, snapshot=None, refusals=()):
        self.previous = last
        self.snapshot = snapshot or SimpleNamespace(
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


def test_nothing_starts_when_the_journal_does_not_authorize(tmp_path):
    async def scenario():
        for last in (
            None,  # the operator's STOP leaves no unfinished run
            previous(state=RunState.FAULTED),
            previous(autonomy="observation"),
            previous(activity=ActivityState.PAUSED),
        ):
            runtime, supervisor = host(last)
            await Resume(runtime, resume_file(tmp_path), FAST).run()
            assert supervisor.calls == []

    asyncio.run(scenario())


def test_a_clean_shutdown_marker_is_single_use(tmp_path):
    async def scenario():
        path = resume_file(tmp_path)
        mark_clean(path, "old-run", BASE.partition)
        runtime, supervisor = host(None)
        await Resume(runtime, path, FAST).run()
        assert len(supervisor.calls) == 1
        assert json.loads(path.read_text(encoding="utf-8"))["clean_run_id"] is None
        runtime, supervisor = host(None)
        await Resume(runtime, path, FAST).run()  # the next boot finds no marker
        assert supervisor.calls == []

    asyncio.run(scenario())


def test_a_marker_that_cannot_be_consumed_means_no_resume(tmp_path, monkeypatch):
    async def scenario():
        path = resume_file(tmp_path)
        mark_clean(path, "old-run", BASE.partition)

        def broken(path):
            raise OSError("synthetic sharing violation")

        monkeypatch.setattr(module, "consume", broken)
        runtime, supervisor = host(None)
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


def running(**changes):
    values = dict(
        state=RunState.ACTIVE,
        autonomy="autonomous",
        activity=ActivityState.OPEN,
        run_id="r1",
        scope=BASE,
        version=9,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_close_remembers_only_a_clean_shutdown_of_a_running_autonomous_work_persona(tmp_path):
    async def scenario():
        test_scope = BASE.model_copy(update={"memory_mode": "test"})
        cases = {
            "running": (running(), "r1"),
            "stopped": (running(state=RunState.STOPPED), None),  # the operator's STOP
            "observation": (running(autonomy="observation"), None),
            "paused": (running(activity=ActivityState.PAUSED), None),
            "test": (running(scope=test_scope), None),
        }
        for name, (snapshot, expected) in cases.items():
            path = resume_file(tmp_path / name)
            runtime, _ = host(None, snapshot=snapshot)
            resume = Resume(runtime, path, FAST)
            resume.attach()
            await resume.close()
            marker = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            assert marker.get("clean_run_id") == expected, name

    asyncio.run(scenario())


def test_a_failed_marker_write_at_shutdown_is_not_an_error_just_no_resume(tmp_path, monkeypatch):
    async def scenario():
        def broken(path, run_id, partition):
            raise OSError("synthetic")

        monkeypatch.setattr(module, "mark_clean", broken)
        runtime, _ = host(None, snapshot=running())
        resume = Resume(runtime, resume_file(tmp_path), FAST)
        resume.attach()
        await resume.close()  # must not raise

    asyncio.run(scenario())
