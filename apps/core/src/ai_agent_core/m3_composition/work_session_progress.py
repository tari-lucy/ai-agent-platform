"""Read-only progress of a work conversation's current turn (no second queue or registry).

Derived from the session owner (SessionBook), its WorkAttempt, the job's state and the LLM
journal at read time. Only confirmed facts: recorded times, model call counts, the files the
turn saved through the file owner and its clipped tool journal while the turn's basis is
released. No prompt, message text or secret is read into it. Reading it never submits,
retries or acknowledges anything.
"""

from typing import TYPE_CHECKING, Literal

from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.agent_workplace import ManagedFile, WorkAttempt
from ai_agent_contracts.panel import (
    WorkSessionDraft,
    WorkSessionEntry,
    WorkSessionModelCalls,
    WorkSessionProgress,
    WorkSessionResultFile,
)
from ai_agent_contracts.states import OperationState

from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime

type State = Literal[
    "waiting_start", "queued", "running", "reply_ready", "done", "failed", "cancelled", "unknown"
]
RUNNING = {OperationState.RUNNING}
ENDED: dict[OperationState, State] = {
    OperationState.SUCCEEDED: "done",
    OperationState.FAILED: "failed",
    OperationState.REJECTED: "failed",
    OperationState.CANCELLED: "cancelled",
    OperationState.EXPIRED: "cancelled",
    OperationState.OUTCOME_UNKNOWN: "unknown",
}
ENDED_ATTEMPT: dict[str, State] = {
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "unknown": "unknown",
}
QUEUED = {
    OperationState.PROPOSED,
    OperationState.AWAITING_APPROVAL,
    OperationState.READY,
    OperationState.QUEUED,
    OperationState.WAITING,
}


async def _calls(rt: Runtime, attempt: WorkAttempt) -> WorkSessionModelCalls:
    rows = [
        row
        for row in await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(
            attempt.scope
        )
        # The executor journals each call as "<job operation>.<call digest>".
        if row.operation_id.startswith(attempt.operation_id + ".")
    ]
    return WorkSessionModelCalls(
        completed=sum(row.status == "completed" for row in rows),
        failed=sum(row.status in ("failed", "incomplete", "cancelled", "unknown") for row in rows),
        running=sum(row.status == "running" for row in rows),
        last_at=max((row.completed_at or row.started_at for row in rows), default=None),
    )


async def _files(rt: Runtime, attempt: WorkAttempt) -> list[WorkSessionResultFile]:
    found: list[WorkSessionResultFile] = []
    repo = rt.store.repository(ManagedFile, PRINCIPALS[Owner.CATALOGUE], attempt.scope)
    for ref in attempt.result_files[:50]:
        entry = await repo.get(ref.artifact_id)
        if entry is None or entry.state != "active":
            continue  # deleted or blocked since: not offered as a result
        found.append(
            WorkSessionResultFile(
                file_id=entry.id,
                version=ref.version,
                title=entry.title,
                path=entry.path,
                current=entry.reference == ref,
            )
        )
    return found


async def _error(rt: Runtime, attempt: WorkAttempt) -> str | None:
    """The owner-recorded error code of the job's last attempt, if there is one."""
    loaded = await rt.jobs.store.load(attempt.operation_id, attempt.scope)
    if loaded is None or loaded[0].lease is None:
        return None
    try:
        last = await rt.jobs.store.attempt(
            attempt.operation_id, loaded[0].lease.attempt_id, attempt.scope
        )
    except Exception:  # noqa: BLE001 - diagnostics are optional, never a failure of the view
        return None
    return None if last is None or last.error is None else last.error.value


async def progress(
    rt: Runtime,
    session: WorkSession,
    entries: list[WorkSessionEntry],
    pending: WorkSessionDraft | None,
) -> WorkSessionProgress | None:
    from .activity import book

    waiting = [
        e for e in entries if e.speaker == "operator" and e.sequence > session.processed_sequence
    ]
    # The latest turn of the conversation, unless a newer operator message has not been taken
    # yet: then that message is the current one (a delivered reply is itself a message).
    attempt = max(
        await book(rt).session_attempts(session.scope, session.id),
        key=lambda a: (a.session_sequence or 0, a.attempt_no),
        default=None,
    )
    if attempt is not None and waiting and (attempt.session_sequence or 0) < waiting[-1].sequence:
        attempt = None
    state: State
    if attempt is None:
        if pending is not None or not waiting or session.state not in ("open", "waiting"):
            return None
        # Queued only when the active executor is this conversation's person/area; another
        # person's message waits for its own activation, never for someone else's run.
        run = rt.supervisor.snapshot
        mine = run.state.value == "ACTIVE" and run.scope.partition == session.scope.partition
        state = "queued" if mine else "waiting_start"
        return WorkSessionProgress(state=state, since=waiting[-1].at)
    if pending is not None:
        state = "reply_ready"
    elif attempt.state in ("prepared", "submitted"):
        loaded = await rt.jobs.store.load(attempt.operation_id, attempt.scope)
        status = None if loaded is None else loaded[0].status
        if status in RUNNING:
            state = "running"
        elif status is None or status in QUEUED:
            state = "queued"
        else:
            # Terminal job, attempt not reconciled yet: report the job's own outcome.
            state = ENDED.get(status, "unknown")
    else:
        state = ENDED_ATTEMPT[attempt.state]
    if state == "done" and not attempt.result_files:
        return None  # the reply is in the history with its journal; nothing more to report
    from .work_session_journal import released

    journal = (attempt.tool_steps or attempt.tool_steps_omitted) and await released(rt, attempt)
    return WorkSessionProgress(
        state=state,
        since=attempt.updated_at if state != "running" else attempt.created_at,
        attempt_no=attempt.attempt_no,
        model_calls=await _calls(rt, attempt),
        files=await _files(rt, attempt),
        error_code=await _error(rt, attempt)
        if state in ("failed", "cancelled", "unknown")
        else None,
        steps=attempt.tool_steps if journal else [],
        steps_omitted=attempt.tool_steps_omitted if journal else 0,
    )
