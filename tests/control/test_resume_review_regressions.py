"""Independent D88 acceptance regressions. Candidate c578038 deliberately fails some.

Synthetic WORK hosts and the real Supervisor; no personal host, Docker or paid calls.
"""

import asyncio
import ctypes
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from ai_agent_contracts.api import ControlArguments
from ai_agent_contracts.states import RunState
from test_supervisor import SCOPE, command, fixture, stop_command

from ai_agent_core.local_backup import cli as backup_cli
from ai_agent_core.local_host import resume as module

WORK = SCOPE.model_copy(update={"memory_mode": "work"})
FAST = module.ResumeSettings(delay=0, interval=0, attempts=2)


async def active_host(tmp_path):
    owner, authority, *_ = fixture(scope=WORK)
    await owner.recover()
    runtime = SimpleNamespace(supervisor=owner, running=True)
    path = module.resume_file(tmp_path)
    resume = module.Resume(runtime, path, FAST)
    await resume.run()
    await owner.control(authority, command(owner))
    assert owner.snapshot.state == RunState.ACTIVE and module.read(path)
    return owner, authority, resume, path


@contextmanager
def deny_delete(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing violation")
def test_stop_disarms_resume_even_if_windows_reader_prevents_atomic_replace(tmp_path):
    async def scenario():
        owner, authority, resume, path = await active_host(tmp_path)
        with deny_delete(path):
            owner.stop(authority, stop_command(owner))
        assert owner.snapshot.state == RunState.STOPPED
        await resume.close()
        # This is what the next process trusts. An explicit STOP must win durably.
        assert not module.read(path), "STOP accepted, but next boot still sees resume=true"

    asyncio.run(scenario())


def test_operator_switch_to_observation_disarms_autonomous_resume(tmp_path):
    async def scenario():
        owner, authority, resume, path = await active_host(tmp_path)
        await owner.control(authority, command(owner, "agent.pause", "pause-for-observation"))
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
        await resume.close()
        assert not module.read(path), "Observation mode must not turn autonomous on reboot"

    asyncio.run(scenario())


def test_real_supervisor_stop_path_has_no_filesystem_write(tmp_path, monkeypatch):
    async def scenario():
        owner, authority, resume, _ = await active_host(tmp_path)
        inside_stop, writes = False, []
        original = module.write

        def write(path, wanted):
            if inside_stop:
                writes.append(wanted)
            original(path, wanted)

        monkeypatch.setattr(module, "write", write)
        inside_stop = True
        owner.stop(authority, stop_command(owner))
        inside_stop = False
        await resume.close()
        assert writes == [], "Supervisor.stop synchronously calls disk IO through its observer"

    asyncio.run(scenario())


def test_restore_disarms_resume_before_calling_restore_owner(tmp_path, monkeypatch):
    path = module.resume_file(tmp_path)
    module.write(path, True)
    digest = "a" * 64

    async def preview(backup_id):
        return SimpleNamespace(digest=digest)

    async def restore(preview, decision):
        assert not module.read(path), "Restore begins with autonomous restart still armed"
        raise RuntimeError("synthetic interruption after restore starts")

    @contextmanager
    def local(arguments, *, offline):
        yield SimpleNamespace(service=SimpleNamespace(preview_restore=preview, restore=restore))

    monkeypatch.setattr(backup_cli, "_local", local)
    arguments = SimpleNamespace(
        command="restore",
        root=tmp_path,
        backup_id="synthetic",
        digest=digest,
        decision_id="synthetic-decision",
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        asyncio.run(backup_cli._run(arguments))


def test_resume_does_not_create_autonomous_test_run(tmp_path):
    async def scenario():
        owner, *_ = fixture()  # TEST partition, like candidate's own positive test
        await owner.recover()
        path = module.resume_file(tmp_path)
        module.write(path, True)
        resume = module.Resume(SimpleNamespace(supervisor=owner, running=True), path, FAST)
        await resume.run()
        await resume.close()
        assert owner.snapshot.state == RunState.STOPPED, "Resume must be restricted to WORK"

    asyncio.run(scenario())


def test_operator_stop_during_delay_cancels_start_on_real_supervisor(tmp_path):
    async def scenario():
        owner, authority, *_ = fixture(scope=WORK)
        await owner.recover()
        path = module.resume_file(tmp_path)
        module.write(path, True)
        resume = module.Resume(
            SimpleNamespace(supervisor=owner, running=True),
            path,
            module.ResumeSettings(delay=0.02, interval=0, attempts=2),
        )
        task = asyncio.create_task(resume.run())
        await asyncio.sleep(0)
        owner.stop(authority, stop_command(owner))
        await task
        await resume.close()
        assert owner.snapshot.state == RunState.STOPPED and not module.read(path)

    asyncio.run(scenario())
