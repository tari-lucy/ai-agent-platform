"""Opt-in resume after a restart (operator decision, doc07 D88).

By default a restart, a sleep or a restore never starts the agent. When the operator turns
``resume_after_restart`` on, the host starts her again after a restart if, and only if, the
previous process left her running on her own. The durable truth is the Supervisor's own
journal, which already records how the previous run ended and which an operator STOP updates
through the Supervisor's own path. Nothing is written in the STOP path.

The journal returns, at load, only runs that were not finished (a stopped run frees its
slot), so two cases are told apart:

- the previous process died (a crash, a power loss): the journal still has her WORK run
  STARTING/ACTIVE, autonomous with open activity (not paused, not observation): resume;
- the previous process shut down cleanly (a reboot, ``systemctl stop``) while she ran: the
  journal has nothing, but a single-use marker written at that shutdown only, bound to her
  run and partition, says so. The marker is consumed at the next boot.

An operator STOP, a fault, a pause, a switch to observation and a restore never authorize it.
A restore also writes a block: only a run created after the restore may be resumed later.
The resumed START is an ordinary ``agent.start`` command with every check. STOP wins at any
time: a STOP while the host waits to resume cancels the resume.
"""

import asyncio
import json
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
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
    """The block or the marker could not be written: the caller must not go on."""


def resume_file(root: Path) -> Path:
    return root / "data" / "harness" / "resume.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def block_resume(root: Path) -> None:
    """Before a restore: nothing that existed before this moment may be resumed.

    Raises ResumeError if the block cannot be written; the restore must not begin then.
    """
    path = resume_file(root)
    try:
        _store(path, {"blocked_before": datetime.now(UTC).isoformat(), "clean_run_id": None})
    except OSError as error:
        raise ResumeError("RESUME_BLOCK_FAILED") from error


def mark_clean(path: Path, run_id: str, partition: tuple[str, ...]) -> None:
    data = _load(path)
    data.update(
        clean_run_id=run_id,
        clean_partition=list(partition),
        clean_at=datetime.now(UTC).isoformat(),
    )
    _store(path, data)


def consume(path: Path) -> None:
    data = _load(path)
    if data.get("clean_run_id") is not None:
        for key in ("clean_run_id", "clean_partition", "clean_at"):
            data[key] = None
        _store(path, data)


def _moment(value: object) -> datetime | None:
    """A stored time; None when absent. Anything else that is not a time raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("RESUME_TIME_INVALID")
    return datetime.fromisoformat(value)


def authorizes(previous: RunSession | None, base: Scope, data: dict[str, Any]) -> bool:
    """Does the journal's unfinished run, or the clean-shutdown marker, allow a resume?"""
    if base.memory_mode != "work":
        return False
    try:
        blocked = _moment(data.get("blocked_before"))
        at = _moment(data.get("clean_at"))
    except ValueError:
        return False
    if previous is not None:
        # A journal with an unfinished run decides alone; a marker next to it is ignored.
        return (
            previous.state in (RunState.STARTING, RunState.ACTIVE)
            and previous.scope.partition == base.partition
            and previous.scope.memory_mode == "work"
            and previous.autonomy == "autonomous"  # observation is the operator's last word
            and previous.activity == ActivityState.OPEN  # so is a pause
            and (blocked is None or previous.created_at > blocked)  # not older than a restore
        )
    if data.get("clean_run_id") is None or data.get("clean_partition") != list(base.partition):
        return False
    if at is None:
        return False
    return blocked is None or at > blocked


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
        data = _load(self.path)
        verdict = authorizes(supervisor.previous, base, data)
        self.attach()
        if not verdict:
            return
        if data.get("clean_run_id") is not None:
            try:  # the marker is single-use; if it cannot be consumed we do not resume
                await asyncio.to_thread(consume, self.path)
            except OSError:
                LOG.warning("Resume marker could not be consumed: no resume")
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
        """Before the runtime closes: a clean shutdown of a running persona is remembered."""
        if self.unobserve is not None:
            self.unobserve()
            self.unobserve = None
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        run = self.rt.supervisor.snapshot
        if (
            run.state == RunState.ACTIVE
            and run.autonomy == "autonomous"
            and run.activity == ActivityState.OPEN
            and run.scope.memory_mode == "work"
        ):
            with suppress(OSError):  # no marker means no resume: the safe side
                await asyncio.to_thread(mark_clean, self.path, run.run_id, run.scope.partition)
