"""Resume after a restart against the real Supervisor: the START it sends is accepted as is."""

import asyncio
from types import SimpleNamespace

from ai_agent_contracts.states import RunState
from test_supervisor import fixture, stop_command

from ai_agent_core.local_host.resume import Resume, ResumeSettings, read, resume_file, write


def test_a_remembered_run_resumes_and_a_later_stop_is_remembered(tmp_path):
    async def scenario():
        owner, authority, _clock, _journal, _readiness = fixture()
        await owner.recover()
        runtime = SimpleNamespace(supervisor=owner, running=True)
        path = resume_file(tmp_path)
        write(path, True)  # the previous process was running her when the power went off
        await Resume(runtime, path, ResumeSettings(delay=0, interval=0, attempts=2)).run()
        assert owner.snapshot.state == RunState.ACTIVE and owner.snapshot.autonomy == "autonomous"
        assert read(path)
        owner.stop(authority, stop_command(owner))  # the operator's STOP
        assert owner.snapshot.state in (RunState.STOPPING, RunState.STOPPED)
        assert not read(path)  # and the next restart leaves her stopped

    asyncio.run(scenario())


def test_without_a_remembered_run_the_host_stays_stopped(tmp_path):
    async def scenario():
        owner, _authority, _clock, _journal, _readiness = fixture()
        await owner.recover()
        runtime = SimpleNamespace(supervisor=owner, running=True)
        await Resume(runtime, resume_file(tmp_path), ResumeSettings(delay=0)).run()
        assert owner.snapshot.state == RunState.STOPPED

    asyncio.run(scenario())
