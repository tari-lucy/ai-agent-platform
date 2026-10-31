"""Independent Windows regressions for 78b26ef; desired invariants, intentionally red."""

import asyncio
import ctypes
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from ai_agent_contracts.states import RunState
from test_supervisor import SCOPE, command, fixture, stop_command

from ai_agent_core.local_host import resume as module

WORK = SCOPE.model_copy(update={"memory_mode": "work"})
FAST = module.ResumeSettings(delay=0, interval=0, attempts=1)


async def boot(root, journal=None):
    owner, authority, _, journal, _ = fixture(journal=journal, scope=WORK)
    await owner.recover()
    resume = module.Resume(
        SimpleNamespace(supervisor=owner, running=True), module.resume_file(root), FAST
    )
    return owner, authority, journal, resume


@contextmanager
def locked(path, *, share=1):
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
    handle = kernel.CreateFileW(str(path), 0x80000000, share, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows sharing violation")
def test_unconsumable_marker_cannot_survive_a_later_operator_stop(tmp_path):
    async def scenario():
        first, authority, journal, resume = await boot(tmp_path)
        await resume.run()
        await first.control(authority, command(first))
        await resume.close()
        first.shutdown()
        await first.flush()

        with locked(module.resume_file(tmp_path)):
            second, authority, _, resume = await boot(tmp_path, journal)
            await resume.run()  # cannot replace marker: correctly stays stopped this boot
            assert second.snapshot.state == RunState.STOPPED
            second.stop(authority, stop_command(second))
            await second.flush()
            await resume.close()

        third, _, _, resume = await boot(tmp_path, journal)
        await resume.run()
        state = third.snapshot.state
        await resume.close()
        assert state == RunState.STOPPED, "a stale marker overrode a durable operator STOP"

    asyncio.run(scenario())


def test_stop_while_clean_marker_is_written_wins_on_the_next_boot(tmp_path, monkeypatch):
    async def scenario():
        owner, authority, journal, resume = await boot(tmp_path)
        await resume.run()
        await owner.control(authority, command(owner))
        entered, release = threading.Event(), threading.Event()
        original = module.mark_clean

        def delayed(*args):
            entered.set()
            assert release.wait(5), "test did not release marker writer"
            original(*args)

        monkeypatch.setattr(module, "mark_clean", delayed)
        closing = asyncio.create_task(resume.close())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            owner.stop(authority, stop_command(owner))
            await owner.flush()
            assert owner.snapshot.state == RunState.STOPPED
        finally:
            release.set()
            await closing
        monkeypatch.setattr(module, "mark_clean", original)
        owner.shutdown()
        await owner.flush()
        restarted, _, _, resume = await boot(tmp_path, journal)
        await resume.run()
        state = restarted.snapshot.state
        await resume.close()
        assert state == RunState.STOPPED, "close wrote authorization after an operator STOP"

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows read sharing violation")
def test_unreadable_restore_block_denies_resume(tmp_path):
    async def scenario():
        owner, authority, journal, resume = await boot(tmp_path)
        await resume.run()
        await owner.control(authority, command(owner))
        module.block_resume(tmp_path)
        data = module._load(module.resume_file(tmp_path))
        assert not module.authorizes(owner.snapshot, WORK, data)
        with locked(module.resume_file(tmp_path), share=0):
            restarted, _, _, resume = await boot(tmp_path, journal)
            await resume.run()
            state = restarted.snapshot.state
            await resume.close()
        assert state == RunState.STOPPED, "a read error silently discarded the restore block"

    asyncio.run(scenario())
