"""Opt-in resume after a restart (operator decision, doc07 D88).

By default a restart, a sleep or a restore never starts the agent. When the operator turns
``resume_after_restart`` on, the host remembers whether the persona was running on her own
(autonomous) and, after a restart, starts her again with the same START the operator would
press. The memory is a small local file:

- the persona running autonomously writes ``resume: true``;
- an operator STOP, a fault or a restore writes/leaves ``resume: false``;
- a clean process shutdown (a reboot, ``systemctl stop``) is not an operator decision: the
  file keeps ``true``, so the next boot resumes;
- the resumed START is an ordinary ``agent.start`` command: epoch, admission, budget, STOP
  and every other check apply as for a click in the panel.

Nothing here relaxes STOP: it is independent, always wins, and clears the file.
"""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_contracts.api import ControlArguments, ControlCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import RunSession
from ai_agent_contracts.states import ErrorCode, RunState

if TYPE_CHECKING:
    from ai_agent_core.m3_composition.service import Runtime

LOG = logging.getLogger(__name__)

# Only these refusals mean "not yet": the host is still settling or Docker is not ready.
RETRY = {ErrorCode.RESOURCE_BUSY, ErrorCode.SERVICE_UNAVAILABLE, ErrorCode.STALE_CONTEXT}


def resume_file(root: Path) -> Path:
    return root / "data" / "harness" / "resume.json"


def clear(root: Path) -> None:
    """After a restore (or any decision to stay stopped): nothing is resumed."""
    write(resume_file(root), False)


def read(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    return isinstance(data, dict) and data.get("resume") is True


def write(path: Path, wanted: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"resume": wanted, "at": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


@dataclass(frozen=True)
class ResumeSettings:
    delay: float = 45.0  # let Docker and the network settle after a boot
    interval: float = 60.0  # between refused attempts
    attempts: int = 6


class Resume:
    def __init__(self, runtime: Runtime, path: Path, settings: ResumeSettings | None = None):
        self.rt, self.path = runtime, path
        self.settings = settings or ResumeSettings()
        self.task: asyncio.Task[None] | None = None
        self.remembered: bool | None = None
        self.priming = False
        self.unobserve: Callable[[], None] | None = None

    # -- remembering -------------------------------------------------------------------------

    def _seen(self, run: RunSession, elapsed: float) -> None:
        if self.priming or not self.rt.running:
            return  # a process shutdown is not an operator decision
        wanted = (
            run.state in (RunState.STARTING, RunState.ACTIVE)
            and run.autonomy == "autonomous"
            and run.scope.memory_mode == "work"
        )
        stopped = run.state in (RunState.STOPPING, RunState.STOPPED, RunState.FAULTED)
        if not (wanted or stopped) or wanted == self.remembered:
            return
        try:
            write(self.path, wanted)
            self.remembered = wanted
        except OSError:
            LOG.warning("Resume memory could not be written")

    def attach(self) -> None:
        # observe() delivers the current state at once: that is not a decision of anybody.
        self.priming = True
        try:
            self.unobserve = self.rt.supervisor.observe(self._seen)
        finally:
            self.priming = False

    # -- resuming ----------------------------------------------------------------------------

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        wanted = read(self.path)
        self.attach()  # from now on a STOP or a fault is remembered
        if not wanted:
            return
        await asyncio.sleep(self.settings.delay)
        for attempt in range(1, self.settings.attempts + 1):
            run = self.rt.supervisor.snapshot
            if run.state != RunState.STOPPED:
                return  # somebody started it already (the operator), or a fault: not our call
            if not read(self.path):
                return  # an operator STOP happened while we waited
            agent = run.scope.agent_id
            if agent is None:
                return
            # The same scope a click in the panel carries: the persona, not the old run.
            scope = Scope(
                workspace_id=run.scope.workspace_id,
                persona_id=run.scope.persona_id,
                memory_space_id=run.scope.memory_space_id,
                memory_mode=run.scope.memory_mode,
                agent_id=agent,
            )
            command = ControlCommand(
                type="agent.start",
                request_id="resume-" + uuid4().hex,
                scope=scope,
                target=agent,
                expected_version=run.version,
                arguments=ControlArguments(autonomy="autonomous"),
            )
            now = self.rt.supervisor.clock.elapsed()
            authority = ControlAuthority("operator", frozenset({run.scope.partition}), now + 120.0)
            try:
                await self.rt.supervisor.control(authority, command)
            except ContractError as error:
                code = error.error.code
                LOG.warning("Resume after restart refused: %s (attempt %d)", code.value, attempt)
                if code not in RETRY:
                    return
                await asyncio.sleep(self.settings.interval)
                continue
            LOG.warning("Resumed after restart: the persona runs again")
            return
        LOG.warning("Resume after restart gave up: START by hand")

    async def close(self) -> None:
        if self.unobserve is not None:
            self.unobserve()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
