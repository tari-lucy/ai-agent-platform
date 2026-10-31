"""Opt-in resume after a restart: remembered by the host, never by a shutdown or a restore."""

import asyncio
import json
from types import SimpleNamespace

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.local_host.resume import (
    Resume,
    ResumeSettings,
    clear,
    read,
    resume_file,
    write,
)

SCOPE = Scope(
    workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work", agent_id="primary"
)
FAST = ResumeSettings(delay=0, interval=0, attempts=3)


def run(state: RunState, autonomy: str = "autonomous", version: int = 7, scope: Scope = SCOPE):
    return SimpleNamespace(state=state, autonomy=autonomy, version=version, scope=scope)


class Supervisor:
    def __init__(self, snapshot, refusals=()):
        self.snapshot, self.refusals, self.calls = snapshot, list(refusals), []
        self.observers = []
        self.clock = SimpleNamespace(elapsed=lambda: 100.0)

    def observe(self, callback):
        callback(self.snapshot, 0.0)  # like the real Supervisor: the current state at once
        self.observers.append(callback)
        return lambda: self.observers.remove(callback)

    async def control(self, authority, command):
        self.calls.append((authority, command))
        if self.refusals:
            raise ContractError(self.refusals.pop(0))
        return SimpleNamespace()

    def emit(self, snapshot):
        self.snapshot = snapshot
        for callback in tuple(self.observers):
            callback(snapshot, 0.0)


def host(tmp_path, snapshot=None, refusals=(), running=True):
    supervisor = Supervisor(snapshot or run(RunState.STOPPED, "observation"), refusals)
    runtime = SimpleNamespace(supervisor=supervisor, running=running)
    return runtime, supervisor, resume_file(tmp_path)


def test_a_running_persona_is_remembered_and_stop_or_a_fault_forgets_it(tmp_path):
    async def scenario():
        runtime, supervisor, path = host(tmp_path)
        resume = Resume(runtime, path, FAST)
        await resume.run()  # nothing remembered yet: nothing to start
        assert supervisor.calls == []
        supervisor.emit(run(RunState.STARTING))
        supervisor.emit(run(RunState.ACTIVE))
        assert read(path)
        supervisor.emit(run(RunState.STOPPING))  # the operator pressed STOP
        assert not read(path)
        supervisor.emit(run(RunState.ACTIVE))
        supervisor.emit(run(RunState.FAULTED))  # a fault needs a hand, not a resume
        assert not read(path)
        supervisor.emit(run(RunState.ACTIVE, autonomy="observation"))  # watching is not living
        assert not read(path)

    asyncio.run(scenario())


def test_a_process_shutdown_is_not_a_decision_and_keeps_the_memory(tmp_path):
    async def scenario():
        runtime, supervisor, path = host(tmp_path)
        resume = Resume(runtime, path, FAST)
        await resume.run()
        supervisor.emit(run(RunState.ACTIVE))
        runtime.running = False  # Runtime.close(): the supervisor then reports STOPPED
        supervisor.emit(run(RunState.STOPPED))
        assert read(path)

    asyncio.run(scenario())


def test_after_a_restart_she_is_started_like_the_operator_would_start_her(tmp_path):
    async def scenario():
        runtime, supervisor, path = host(tmp_path)
        write(path, True)
        await Resume(runtime, path, FAST).run()
        [(authority, command)] = supervisor.calls
        assert command.type == "agent.start" and command.arguments.autonomy == "autonomous"
        assert command.expected_version == 7 and command.scope.partition == SCOPE.partition
        assert command.target == SCOPE.agent_id and command.request_id.startswith("resume-")
        assert authority.principal_id == "operator" and SCOPE.partition in authority.partitions
        assert authority.expires_at > 100.0

    asyncio.run(scenario())


def test_nothing_starts_without_a_remembered_run_or_after_a_stop_or_a_restore(tmp_path):
    async def scenario():
        runtime, supervisor, path = host(tmp_path)
        await Resume(runtime, path, FAST).run()  # no memory file
        write(path, False)
        await Resume(runtime, path, FAST).run()  # the operator stopped her
        write(path, True)
        clear(tmp_path)  # a restore clears the memory
        await Resume(runtime, path, FAST).run()
        path.write_text("{broken", encoding="utf-8")  # unreadable memory means no resume
        await Resume(runtime, path, FAST).run()
        assert supervisor.calls == []

    asyncio.run(scenario())


def test_it_waits_out_a_busy_host_but_gives_up_and_never_overrides_the_operator(tmp_path):
    async def scenario():
        runtime, supervisor, path = host(tmp_path, refusals=[ErrorCode.RESOURCE_BUSY] * 2)
        write(path, True)
        await Resume(runtime, path, FAST).run()
        assert len(supervisor.calls) == 3  # two refusals, then it went through

        runtime, supervisor, path = host(tmp_path, refusals=[ErrorCode.RESOURCE_BUSY] * 9)
        write(path, True)
        await Resume(runtime, path, FAST).run()
        assert len(supervisor.calls) == 3  # attempts are bounded: then START by hand

        runtime, supervisor, path = host(tmp_path, refusals=[ErrorCode.ACCESS_DENIED])
        write(path, True)
        await Resume(runtime, path, FAST).run()
        assert len(supervisor.calls) == 1  # a real refusal is final

        runtime, supervisor, path = host(tmp_path, snapshot=run(RunState.ACTIVE))
        write(path, True)
        await Resume(runtime, path, FAST).run()  # somebody started her already
        assert supervisor.calls == []

    asyncio.run(scenario())


def test_the_memory_is_private_and_atomic(tmp_path):
    path = resume_file(tmp_path)
    write(path, True)
    assert json.loads(path.read_text(encoding="utf-8"))["resume"] is True
    assert not path.with_suffix(".tmp").exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
