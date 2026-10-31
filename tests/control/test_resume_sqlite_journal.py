"""The production SQLite run journal tells a STOP from a process shutdown (doc07 D88)."""

import asyncio
from types import SimpleNamespace

from ai_agent_adapters.control.journal import SQLiteRunJournal
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Grant, Owner
from ai_agent_contracts.states import RunState
from test_supervisor import PRINCIPAL, ROOT, SCOPE, command, fixture, stop_command

from ai_agent_core.local_host.resume import Resume, ResumeSettings, authorizes, resume_file

WORK = SCOPE.model_copy(update={"memory_mode": "work"})


async def open_store(path):
    store = SQLiteStore(path)
    await store.upgrade(ROOT / "migrations")
    await store.provision(WORK, (Grant(PRINCIPAL, Owner.SUPERVISOR, read=True, write=True),))
    return store, SQLiteRunJournal(store, PRINCIPAL, (WORK,))


def test_latest_run_shows_who_ended_it_and_survives_a_restart_on_real_sqlite(tmp_path):
    async def scenario():
        store, journal = await open_store(tmp_path / "run.db")
        try:
            assert await journal.latest(WORK) is None  # nothing ever ran
            owner, authority, *_ = fixture(journal=journal, scope=WORK)
            await owner.recover()
            await owner.control(authority, command(owner))
            owner.shutdown()  # a clean process shutdown while she ran
            await owner.flush()
            assert await journal.load() == []  # a finished run frees the slot...
            closed = await journal.latest(WORK)  # ...but is still the latest record
            assert closed.state == RunState.STOPPED and closed.ended_at is None
            assert authorizes(closed, WORK, None)

            second, authority2, *_ = fixture(journal=journal, scope=WORK)
            await second.recover()
            assert second.previous.id == closed.id  # recovery sees it before it moves on
            runtime = SimpleNamespace(supervisor=second, running=True)
            await Resume(runtime, resume_file(tmp_path / "host"), ResumeSettings(delay=0)).run()
            assert second.snapshot.state == RunState.ACTIVE

            second.stop(authority2, stop_command(second))  # the operator's STOP
            await second.flush()
            stopped = await journal.latest(WORK)
            assert stopped.id == second.snapshot.id and stopped.ended_at is not None
            assert not authorizes(stopped, WORK, None)

            third, *_ = fixture(journal=journal, scope=WORK)
            await third.recover()
            runtime = SimpleNamespace(supervisor=third, running=True)
            await Resume(runtime, resume_file(tmp_path / "host"), ResumeSettings(delay=0)).run()
            assert third.snapshot.state == RunState.STOPPED  # the last decision stands

            other = WORK.model_copy(update={"persona_id": "someone-else"})
            assert await journal.latest(other) is None  # another partition: not ours
        finally:
            await store.close()

    asyncio.run(scenario())
