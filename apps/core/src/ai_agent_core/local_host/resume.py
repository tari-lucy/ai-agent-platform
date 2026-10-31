"""Opt-in resume after a restart (operator decision, doc07 D88).

By default a restart, a sleep or a restore never starts the agent. When the operator turns
``resume_after_restart`` on, the host starts her again after a restart if, and only if, the
previous process left her running on her own. The only authority is the Supervisor's own
journal: the latest run of the persona's WORK partition, exactly as the previous process left
it (``Supervisor.previous``). Nothing is written in the STOP path and no file can outlive an
operator decision.

The latest run authorizes a resume when it was autonomous with open activity (not observation,
not paused) and either

- it is still STARTING/ACTIVE: the process died (a crash, a power loss); or
- it is STOPPED with no ``ended_at``: a STOP always sets ``ended_at``, a clean process
  shutdown (a reboot, ``systemctl stop``) deliberately does not, so nobody ended that run.

A STOP, a fault, a pause, a switch to observation, a newer run of any kind and a restore never
authorize it. The only file is the block a restore writes before it begins: only a run created
after the restore may be resumed afterwards. A block that cannot be read denies the resume.
The resumed START is an ordinary ``agent.start`` command with every check. STOP wins at any
time: a STOP while the host waits to resume cancels the resume.
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
from ai_agent_contracts.states import ActivityState, ErrorCode, RunState

if TYPE_CHECKING:
    from ai_agent_core.m3_composition.service import Runtime

LOG = logging.getLogger(__name__)

# Only these refusals mean "not yet". A readiness failure inside START faults the run: after
# that nothing is retried and the operator starts her by hand.
RETRY = {ErrorCode.RESOURCE_BUSY, ErrorCode.SERVICE_UNAVAILABLE, ErrorCode.STALE_CONTEXT}


class ResumeError(Exception):
    """The block could not be written or read: the caller must not go on."""


def resume_file(root: Path) -> Path:
    return root / "data" / "harness" / "resume.json"


def block_resume(root: Path) -> None:
    """Before a restore: no run that exists before this moment may be resumed.

    Raises ResumeError if the block cannot be written; the restore must not begin then.
    """
    path = resume_file(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"blocked_before": datetime.now(UTC).isoformat()}), encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise ResumeError("RESUME_BLOCK_FAILED") from error


def read_block(path: Path) -> datetime | None:
    """No file: no block. A file that cannot be read or understood is not 'no block'."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ResumeError("RESUME_BLOCK_UNREADABLE") from error
    try:
        data = json.loads(text)
        value = data["blocked_before"]
        if not isinstance(value, str):
            raise TypeError
        return datetime.fromisoformat(value)
    except (ValueError, KeyError, TypeError) as error:
        raise ResumeError("RESUME_BLOCK_INVALID") from error


def authorizes(previous: RunSession | None, base: Scope, blocked: datetime | None) -> bool:
    """Does the persona's latest run, as the previous process left it, allow a resume?"""
    if previous is None or base.memory_mode != "work":
        return False
    if (
        previous.scope.partition != base.partition
        or previous.scope.memory_mode != "work"
        or previous.autonomy != "autonomous"  # observation is the operator's last word
        or previous.activity != ActivityState.OPEN  # so is a pause
    ):
        return False
    if blocked is not None and previous.created_at <= blocked:
        return False  # a run older than the last restore
    if previous.state in (RunState.STARTING, RunState.ACTIVE):
        return True  # the process died while she ran
    # Closed by a process shutdown: a STOP would have set ended_at.
    return previous.state == RunState.STOPPED and previous.ended_at is None


@dataclass(frozen=True)
class ResumeSettings:
    delay: float = 45.0  # let Docker and the network settle after a boot
    interval: float = 60.0  # between refusals that mean "not yet"
    attempts: int = 6


class Resume:
    def __init__(self, runtime: Runtime, path: Path, settings: ResumeSettings | None = None):
        self.rt, self.path = runtime, path
        self.settings = settings or ResumeSettings()
        self.task: asyncio.Task[None] | None = None
        self.unobserve: Callable[[], None] | None = None
        self.priming = False
        self.cancelled = False

    # -- watching for STOP: memory only, no IO ------------------------------------------------

    def _seen(self, run: RunSession, elapsed: float) -> None:
        if self.priming:
            return
        if run.state in (RunState.STOPPING, RunState.STOPPED, RunState.FAULTED):
            self.cancelled = True  # a STOP while we wait wins

    def attach(self) -> None:
        # observe() delivers the current state at once: that is not a decision of anybody.
        self.priming = True
        try:
            self.unobserve = self.rt.supervisor.observe(self._seen)
        finally:
            self.priming = False

    # -- resuming -----------------------------------------------------------------------------

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        supervisor = self.rt.supervisor
        base = supervisor.snapshot.scope
        idle = supervisor.snapshot.run_id  # the closed run this boot started with
        try:
            blocked = read_block(self.path)
        except ResumeError as error:
            LOG.warning("Resume denied: %s", error)  # an unreadable block is not 'no block'
            self.attach()
            return
        verdict = authorizes(supervisor.previous, base, blocked)
        self.attach()
        if not verdict:
            return
        await asyncio.sleep(self.settings.delay)
        for attempt in range(1, self.settings.attempts + 1):
            run = supervisor.snapshot
            if self.cancelled or run.state != RunState.STOPPED or run.run_id != idle:
                return  # the operator acted while we waited
            agent = run.scope.agent_id
            if agent is None or run.scope.memory_mode != "work":
                return
            # The same scope a click in the panel carries: the persona, not the old run.
            scope = Scope(
                workspace_id=run.scope.workspace_id,
                persona_id=run.scope.persona_id,
                memory_space_id=run.scope.memory_space_id,
                memory_mode="work",
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
            now = supervisor.clock.elapsed()
            authority = ControlAuthority("operator", frozenset({run.scope.partition}), now + 120.0)
            try:
                await supervisor.control(authority, command)
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
            self.unobserve = None
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
