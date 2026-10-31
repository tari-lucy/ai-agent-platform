"""Resume after a restart against the real Supervisor and its journal.

A "restart" is a new Supervisor over the same journal: the previous process left it as it was
(STARTING/ACTIVE after a crash; STOPPED with no ended_at after a clean shutdown; STOPPED with
ended_at after an operator STOP). These cover the two independent reviews: no IO in the STOP
path, no state outside the journal that could outlive a STOP, observation, pause, TEST, restore.
"""

import asyncio
from contextlib import contextmanager
from pathlib import Path
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
    await resume.run()  # nothing to resume on the first boot; it only attaches
    await owner.control(authority, command(owner))
    assert owner.snapshot.state == RunState.ACTIVE and owner.snapshot.autonomy == "autonomous"
    return owner, authority, journal, resume


async def restart(journal, tmp_path, scope=WORK):
    """The next process over the same journal, after its Resume ran."""
    owner, authority, _ = await process(journal, scope)
    runtime = SimpleNamespace(supervisor=owner, running=True)
    await Resume(runtime, resume_file(tmp_path), FAST).run()
    return owner, authority


async def clean_shutdown(owner, resume):
    await resume.close()  # serve: before the runtime closes
    owner.shutdown()  # Runtime.close
    await owner.flush()


def test_a_crash_while_she_ran_resumes_her(tmp_path):
    async def scenario():
        _owner, _authority, journal, _resume = await running_process(tmp_path)
        owner, _ = await restart(journal, tmp_path)  # the first process just vanished
        assert owner.snapshot.state == RunState.ACTIVE and owner.snapshot.autonomy == "autonomous"

    asyncio.run(scenario())


def test_a_clean_shutdown_while_she_ran_resumes_her(tmp_path):
    async def scenario():
        owner, _authority, journal, resume = await running_process(tmp_path)
        await clean_shutdown(owner, resume)
        assert owner.snapshot.state == RunState.STOPPED and owner.snapshot.ended_at is None
        second, _ = await restart(journal, tmp_path)
        assert second.snapshot.state == RunState.ACTIVE

    asyncio.run(scenario())


def test_an_operator_stop_is_final_whatever_the_disk_does(tmp_path, monkeypatch):
    async def scenario():
        owner, authority, journal, resume = await running_process(tmp_path)

        def broken(*args, **kwargs):
            raise OSError("synthetic Windows sharing violation")

        monkeypatch.setattr(Path, "write_text", broken)
        monkeypatch.setattr(module.os, "replace", broken)
        owner.stop(authority, stop_command(owner))
        assert owner.snapshot.state == RunState.STOPPED and owner.snapshot.ended_at is not None
        await clean_shutdown(owner, resume)
        monkeypatch.undo()
        owner2, _ = await restart(journal, tmp_path)
        assert owner2.snapshot.state == RunState.STOPPED  # the STOP is in the journal

    asyncio.run(scenario())


def test_the_stop_path_has_no_filesystem_write(tmp_path, monkeypatch):
    async def scenario():
        owner, authority, _journal, resume = await running_process(tmp_path)
        calls = []

        def spy(name, original):
            def wrapper(*args, **kwargs):
                calls.append(name)
                return original(*args, **kwargs)

            return wrapper

        monkeypatch.setattr(Path, "write_text", spy("write_text", Path.write_text))
        monkeypatch.setattr(module.os, "replace", spy("replace", module.os.replace))
        monkeypatch.setattr(module, "block_resume", spy("block", module.block_resume))
        owner.stop(authority, stop_command(owner))  # the observer runs inside this call
        assert calls == [], "Supervisor.stop must not reach the disk through the resume observer"
        await resume.close()

    asyncio.run(scenario())


def test_a_stop_during_shutdown_wins_there_is_no_marker_to_outlive_it(tmp_path):
    async def scenario():
        owner, authority, journal, resume = await running_process(tmp_path)
        await resume.close()  # close() has nothing to write, so a STOP can never race it
        owner.stop(authority, stop_command(owner))
        owner.shutdown()
        await owner.flush()
        second, _ = await restart(journal, tmp_path)
        assert second.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())


def test_a_later_manual_run_and_stop_cancel_an_older_shutdown_for_good(tmp_path):
    async def scenario():
        owner, _authority, journal, resume = await running_process(tmp_path)
        await clean_shutdown(owner, resume)
        # Boot two does not resume (say the setting was off) and the operator works by hand.
        second, authority2, _ = await process(journal)
        await second.control(authority2, command(second, request_id="manual-start"))
        second.stop(authority2, stop_command(second, "manual-stop"))
        await second.flush()
        third, _ = await restart(journal, tmp_path)
        assert third.snapshot.state == RunState.STOPPED  # the last decision is the STOP

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


def test_a_run_that_never_started_leaves_nothing_to_resume(tmp_path):
    async def scenario():
        owner, _authority, journal = await process()
        owner.shutdown()  # an idle host closes: its idle run is saved, observation, never run
        await owner.flush()
        second, _ = await restart(journal, tmp_path)
        assert second.snapshot.state == RunState.STOPPED

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


def test_an_unreadable_restore_block_denies_the_resume(tmp_path, monkeypatch):
    async def scenario():
        _owner, _authority, journal, _resume = await running_process(tmp_path)
        module.block_resume(tmp_path)  # a restore would have blocked everything older

        def locked(self, *args, **kwargs):
            raise PermissionError("synthetic sharing violation")

        monkeypatch.setattr(Path, "read_text", locked)
        owner, _ = await restart(journal, tmp_path)
        assert owner.snapshot.state == RunState.STOPPED  # denied, not 'no block'

    asyncio.run(scenario())


def test_a_restore_block_stops_the_resume_of_every_older_run(tmp_path):
    async def scenario():
        _owner, _authority, journal, _resume = await running_process(tmp_path)
        module.block_resume(tmp_path)
        owner, _ = await restart(journal, tmp_path)
        assert owner.snapshot.state == RunState.STOPPED  # the run is older than the restore

    asyncio.run(scenario())


@contextmanager
def local_owner(service):
    yield SimpleNamespace(service=service)


@pytest.mark.parametrize("fails", [False, True])
def test_restore_blocks_resume_before_the_owner_starts_or_does_not_begin(
    tmp_path, monkeypatch, fails
):
    from ai_agent_core.local_backup import cli as backup_cli
    from ai_agent_core.local_host.layout import HostError

    digest = "a" * 64
    seen = {}

    async def preview(backup_id):
        return SimpleNamespace(digest=digest, blocked=False)

    async def restore(preview, decision):
        seen["blocked"] = module.read_block(resume_file(tmp_path))
        raise RuntimeError("synthetic interruption after the restore started")

    @contextmanager
    def local(arguments, *, offline):
        yield SimpleNamespace(service=SimpleNamespace(preview_restore=preview, restore=restore))

    monkeypatch.setattr(backup_cli, "_local", local)
    arguments = SimpleNamespace(
        command="restore",
        root=tmp_path,
        backup_id="synthetic",
        digest=digest,
        decision_id="decision-1",
    )
    if fails:

        def broken(*args, **kwargs):
            raise OSError("synthetic")

        monkeypatch.setattr(module.os, "replace", broken)
        with pytest.raises(HostError):
            asyncio.run(backup_cli._run(arguments))
        assert seen == {}  # the restore did not begin
    else:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            asyncio.run(backup_cli._run(arguments))
        assert seen["blocked"] is not None  # the block existed before the owner was called
