"""Windows equivalents of the three c3255ea regressions, after marker removal."""

import asyncio
import ctypes
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from ai_agent_contracts.states import RunState
from test_resume_sqlite_journal import WORK, open_store
from test_supervisor import command, fixture, sqlite_dir, stop_command  # noqa: F401

from ai_agent_core.local_host import resume as module

FAST = module.ResumeSettings(delay=0, interval=0, attempts=1)


async def boot(root, journal):
    owner, authority, *_ = fixture(journal=journal, scope=WORK)
    await owner.recover()
    resume = module.Resume(
        SimpleNamespace(supervisor=owner, running=True), module.resume_file(root), FAST
    )
    return owner, authority, resume


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
def test_stop_survives_shutdown_and_locked_resume_file(sqlite_dir, tmp_path):  # noqa: F811
    async def scenario():
        database = sqlite_dir / "stop.db"
        store, journal = await open_store(database)
        try:
            owner, authority, resume = await boot(tmp_path, journal)
            await resume.run()
            await owner.control(authority, command(owner))
            module.block_resume(tmp_path)
            path = module.resume_file(tmp_path)
            # An old restore block must permit this newer run; it cannot mask a bad STOP.
            path.write_text(json.dumps({"blocked_before": "2000-01-01T00:00:00+00:00"}))
            assert module.authorizes(owner.snapshot, WORK, module.read_block(path))
            with locked(path):
                owner.stop(authority, stop_command(owner))
                await owner.flush()
                await resume.close()
                owner.shutdown()
                await owner.flush()
                assert (await journal.latest(WORK)).ended_at is not None
        finally:
            await store.close()
        store, journal = await open_store(database)  # reopen durable SQLite, not a memory cache
        try:
            restarted, _, resume = await boot(tmp_path, journal)
            await resume.run()
            assert restarted.snapshot.state == RunState.STOPPED
            await resume.close()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_stop_between_resume_close_and_runtime_shutdown_is_durable(sqlite_dir, tmp_path):  # noqa: F811
    async def scenario():
        store, journal = await open_store(sqlite_dir / "close.db")
        try:
            owner, authority, resume = await boot(tmp_path, journal)
            await resume.run()
            await owner.control(authority, command(owner))
            await resume.close()  # former writer race: observer is already detached
            owner.stop(authority, stop_command(owner))
            await owner.flush()
            ended = owner.snapshot.ended_at
            assert ended is not None
            owner.shutdown()
            await owner.flush()
            assert (await journal.latest(WORK)).ended_at == ended
            restarted, _, resume = await boot(tmp_path, journal)
            await resume.run()
            assert restarted.snapshot.state == RunState.STOPPED
            await resume.close()
            assert not module.resume_file(tmp_path).exists()  # no authorization file created
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows read sharing violation")
def test_unreadable_restore_block_denies_resume_with_sqlite(sqlite_dir, tmp_path, caplog):  # noqa: F811
    async def scenario():
        store, journal = await open_store(sqlite_dir / "block.db")
        try:
            owner, authority, resume = await boot(tmp_path, journal)
            await resume.run()
            await owner.control(authority, command(owner))
            module.block_resume(tmp_path)
            path = module.resume_file(tmp_path)
            assert not module.authorizes(owner.snapshot, WORK, module.read_block(path))
            with locked(path, share=0):
                with pytest.raises(module.ResumeError, match="RESUME_BLOCK_UNREADABLE"):
                    module.read_block(path)
                restarted, _, resume = await boot(tmp_path, journal)
                await resume.run()
                assert restarted.snapshot.state == RunState.STOPPED
                assert "RESUME_BLOCK_UNREADABLE" in caplog.text
                await resume.close()
        finally:
            await store.close()

    asyncio.run(scenario())
