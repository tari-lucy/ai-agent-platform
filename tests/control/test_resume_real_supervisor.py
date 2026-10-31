"""Resume after a restart against the real Supervisor and its journal.

A "restart" is a new Supervisor over the same journal: the previous process left it as it was
(STARTING/ACTIVE after a crash, STOPPED after an operator STOP or a clean shutdown).
These cover the independent review of c578038: no IO in the STOP path, a STOP that cannot be
overridden by a failed write, observation and pause, TEST scopes, restore.
"""

import asyncio
from types import SimpleNamespace

import pytest
from ai_agent_contracts.api import ControlArguments
from ai_agent_contracts.states import RunState
from test_supervisor import SCOPE, command, fixture, stop_command

from ai_agent_core.local_host import resume as module
from ai_agent_core.local_host.resume import Resume, ResumeSettings, resume_file

WORK = SCOPE.model_copy(update={"memory_mode": "work"})
FAST = ResumeSettings(delay=0, interval=0, attempts=2)


async def process(journal=None, scope=WORK):
    owner, authority, _clock, journal, _ready = fixture(journal=journal, scope=scope)
    await owner.recover()
    return owner, authority, journal


async def running_process(tmp_path):
    """A process with the persona running on her own, her Resume attached, as serve does."""
    owner, authority, journal = await process()
    runtime = SimpleNamespace(supervisor=owner, running=True)
    resume = Resume(runtime, resume_file(tmp_path), FAST)
    await resume.run()  # nothing to resume: first boot; it only attaches
    await owner.control(authority, command(owner))
    assert owner.snapshot.state == RunState.ACTIVE and owner.snapshot.autonomy == "autonomous"
    return owner, authority, journal, resume


async def restart(journal, tmp_path, scope=WORK):
    """The next process over the same journal; returns it after Resume ran."""
    owner, authority, _ = await process(journal, scope)
    runtime = SimpleNamespace(supervisor=owner, running=True)
    resume = Resume(runtime, resume_file(tmp_path), FAST)
    await resume.run()
    return owner, authority


def test_a_crash_while_she_ran_resumes_her(tmp_path):
    async def scenario():
        _owner, _authority, journal, _resume = await running_process(tmp_path)
        owner, _ = await restart(journal, tmp_path)  # the first process just vanished
        assert owner.snapshot.state == RunState.ACTIVE and owner.snapshot.autonomy == "autonomous"

    asyncio.run(scenario())


def test_an_operator_stop_is_not_overridden_even_if_nothing_can_be_written(tmp_path, monkeypatch):
    async def scenario():
        owner, authority, journal, resume = await running_process(tmp_path)

        def broken(*args, **kwargs):
            raise OSError("synthetic Windows sharing violation")

        for name in ("_store", "mark_clean", "consume"):
            monkeypatch.setattr(module, name, broken)
        owner.stop(authority, stop_command(owner))
        assert owner.snapshot.state == RunState.STOPPED
        await owner.flush()
        await resume.close()  # a failed write is only "no resume"
        monkeypatch.undo()
        owner2, _ = await restart(journal, tmp_path)
        assert owner2.snapshot.state == RunState.STOPPED  # the STOP is in the journal

    asyncio.run(scenario())


def test_the_stop_path_has_no_filesystem_write(tmp_path, monkeypatch):
    async def scenario():
        owner, authority, _journal, resume = await running_process(tmp_path)
        calls = []
        for name in ("_store", "mark_clean", "consume", "block_resume"):
            original = getattr(module, name)

            def spy(*args, __original=original, __name=name, **kwargs):
                calls.append(__name)
                return __original(*args, **kwargs)

            monkeypatch.setattr(module, name, spy)
        owner.stop(authority, stop_command(owner))  # the observer runs inside this call
        assert calls == [], "Supervisor.stop must not reach the disk through the resume observer"
        await resume.close()

    asyncio.run(scenario())


def test_switching_to_observation_or_pausing_is_the_operators_last_word(tmp_path):
    async def scenario():
        owner, authority, journal, _resume = await running_process(tmp_path)
        await owner.control(authority, command(owner, "agent.pause", "pause-1"))
        await owner.flush()
        paused, _ = await restart(journal, tmp_path)  # the process died while she was paused
        assert paused.snapshot.state == RunState.STOPPED

        owner, authority, journal, _resume = await running_process(tmp_path)
        await owner.control(authority, command(owner, "agent.pause", "pause-2"))
        await owner.flush()
        await owner.control(
            authority,
            command(
                owner,
                "agent.resume",
                "observe-only",
                arguments=ControlArguments(autonomy="observation"),
            ),
        )
        assert owner.snapshot.autonomy == "observation"
        await owner.flush()
        observed, _ = await restart(journal, tmp_path)
        assert observed.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_a_clean_shutdown_while_she_ran_resumes_her_once_and_a_stop_does_not(tmp_path):
    async def scenario():
        owner, authority, journal, resume = await running_process(tmp_path)
        await resume.close()  # serve: before the runtime closes
        owner.shutdown()  # Runtime.close
        await owner.flush()
        assert owner.snapshot.state == RunState.STOPPED
        second, authority2 = await restart(journal, tmp_path)
        assert second.snapshot.state == RunState.ACTIVE

        runtime = SimpleNamespace(supervisor=second, running=True)
        closing = Resume(runtime, resume_file(tmp_path), FAST)
        second.stop(authority2, stop_command(second))  # the operator stops her
        await second.flush()
        await closing.close()  # nothing to remember: she is stopped
        third, _ = await restart(journal, tmp_path)
        assert third.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_a_second_boot_does_not_resume_on_a_consumed_marker(tmp_path):
    async def scenario():
        owner, _authority, journal, resume = await running_process(tmp_path)
        await resume.close()
        owner.shutdown()
        await owner.flush()
        second, _ = await restart(journal, tmp_path)
        assert second.snapshot.state == RunState.ACTIVE
        second.shutdown()  # this process closes without Resume.close: no new marker
        await second.flush()
        third, _ = await restart(journal, tmp_path)
        assert third.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_resume_never_creates_an_autonomous_test_run(tmp_path):
    async def scenario():
        owner, authority, journal = await process(scope=SCOPE)  # TEST partition
        await owner.control(authority, command(owner))  # a TEST run, started by the operator
        owner2, _ = await restart(journal, tmp_path, scope=SCOPE)
        assert owner2.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_a_fault_needs_a_hand(tmp_path):
    async def scenario():
        owner, _authority, journal, _resume = await running_process(tmp_path)
        owner.critical_failure()
        await owner.flush()
        faulted, _ = await restart(journal, tmp_path)
        assert faulted.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_a_stop_during_the_wait_cancels_the_resume(tmp_path):
    async def scenario():
        _owner, _authority, journal, _resume = await running_process(tmp_path)
        owner, authority, _ = await process(journal)
        runtime = SimpleNamespace(supervisor=owner, running=True)
        slow = ResumeSettings(delay=0.05, interval=0, attempts=2)
        resume = Resume(runtime, resume_file(tmp_path), slow)
        task = asyncio.create_task(resume.run())
        await asyncio.sleep(0.01)
        owner.stop(authority, stop_command(owner))  # STOP wins over the pending resume
        await task
        await resume.close()
        assert owner.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_restore_blocks_resume_before_the_owner_starts_or_does_not_begin(
    tmp_path, monkeypatch, fails
):
    from types import SimpleNamespace as NS

    from ai_agent_core.local_backup import cli as backup_cli
    from ai_agent_core.local_host.layout import HostError

    digest = "a" * 64
    seen = {}

    async def preview(backup_id):
        return NS(digest=digest, blocked=False)

    async def restore(preview, decision):
        seen["blocked"] = module._load(resume_file(tmp_path)).get("blocked_before")
        raise RuntimeError("synthetic interruption after the restore started")

    from contextlib import contextmanager

    @contextmanager
    def local(arguments, *, offline):
        yield NS(service=NS(preview_restore=preview, restore=restore))

    monkeypatch.setattr(backup_cli, "_local", local)
    arguments = NS(
        command="restore",
        root=tmp_path,
        backup_id="synthetic",
        digest=digest,
        decision_id="decision-1",
    )
    if fails:

        def broken(path, data):
            raise OSError("synthetic")

        monkeypatch.setattr(module, "_store", broken)
        with pytest.raises(HostError):
            asyncio.run(backup_cli._run(arguments))
        assert seen == {}  # the restore did not begin
    else:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            asyncio.run(backup_cli._run(arguments))
        assert seen["blocked"]  # the block was written before the owner was called
