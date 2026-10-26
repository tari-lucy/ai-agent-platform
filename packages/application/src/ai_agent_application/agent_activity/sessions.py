"""Activity-owned logical sessions. No runner, model call, rights grant or auto-start.

Trusted composition stores text through the existing Source owner BEFORE append.
Only references are stored here. R2 releases bodies/history through their owners.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

from ai_agent_contracts.agent_sessions import (
    SessionReplyRef,
    SessionTicketRef,
    WakeTimer,
    WorkSession,
    WorkSessionCloseArguments,
    WorkSessionMessage,
    WorkSessionMessageArguments,
    WorkSessionOpenArguments,
    WorkSessionRenameArguments,
)
from ai_agent_contracts.api import (
    CreateCommand,
    EditCommand,
    MessageArguments,
    MessageCommand,
    Receipt,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.memory import derived_id
from ai_agent_application.storage import Owner, Principal, RecordAccess, Storage

from .book import ActivityBook, area

# Messages that ask for a turn: the operator's and the agent's own wake-ups (W7).
INPUTS = frozenset({"operator", "wake"})
MAX_PENDING_TIMERS = 16


def message_id(scope: Scope, session_id: str, sequence: int) -> str:
    return derived_id("work-message", area(scope), session_id, str(sequence))


class SessionBook:
    """CAS + request replay of ordered operator input, independent of run state.

    authorize is the caller's live fence (authenticated operator for intake,
    current runtime admission for the delivery hook); verify_source is the
    memory/retention owner check of the exact ref. Neither may be omitted.
    This class is not an agent-facing tool. A character reference is accepted only
    by record_delivered with an independently checked delivery from its owner.
    """

    def __init__(
        self,
        store: Storage,
        principal: Principal,
        clock: Callable[[], datetime],
        *,
        authorize: Callable[[Scope], None],
        verify_source: Callable[[Scope, SourceRef, str], Awaitable[None]],
        verify_delivery: Callable[[Scope, str, SourceRef], Awaitable[None]] | None = None,
        readers: tuple[RecordAccess, ...] = (),
        max_sessions: int = 20,
        max_messages: int = 2000,
    ) -> None:
        if (
            principal.owner != Owner.ACTIVITY
            or not 1 <= max_sessions <= 200
            or not (1 <= max_messages <= 100000)
        ):
            raise ValueError("SESSION_LIMITS_OR_OWNER")
        self.store, self.principal, self.clock = store, principal, clock
        self.authorize, self.verify_source, self.readers = authorize, verify_source, readers
        self.verify_delivery = verify_delivery
        self.max_sessions, self.max_messages = max_sessions, max_messages

    async def get(self, scope: Scope, session_id: str) -> WorkSession | None:
        return await self.store.repository(WorkSession, self.principal, area(scope)).get(session_id)

    async def reset_context(
        self, scope: Scope, session_id: str, request_id: str, *, expected_version: int
    ) -> None:
        """Trusted reset: retain history/progress/tasks, discard stale runtime context.

        Used by offline restore and by the session driver when part of the stored context
        (an undelivered draft's basis) became unavailable. No message is marked delivered or
        processed, and uncertain attempts stay uncertain. The caller's fence proves its
        ownership (exclusive offline host or current runtime); not a model tool.
        """
        scope = area(scope)
        request = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=session_id,
            type="dialogue.message",
            arguments=MessageArguments(text="reset context"),
        )
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, request) as unit:
                if unit.replay is not None:
                    return
                current = await self.get(scope, session_id)
                if current is None or current.version != expected_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                revised = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_at": self.clock(),
                        "generation": current.generation + 1,
                        "checkpoint": None,
                        "pending_reply": None,
                        "context_sources": [],
                        "context_files": [],
                    }
                )
                await unit.put(revised, expected_version=current.version)
                await unit.complete(
                    Receipt(
                        request_id=request_id,
                        scope=scope,
                        status="completed",
                        resource_id=current.id,
                        version=revised.version,
                    )
                )

    async def invalidate(
        self,
        scope: Scope,
        session_id: str,
        request_id: str,
        *,
        expected_version: int,
        reason: Literal["message_unavailable"],
    ) -> Receipt:
        """Terminal stop: an unprocessed operator message is no longer available.

        It is never executed and no later input is accepted here; the operator continues in a
        new conversation. Draft, checkpoint and runtime context are discarded, tasks stay.
        Called by trusted composition (session driver), not a model tool; replay is a no-op.
        """
        scope = area(scope)
        request = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=session_id,
            type="dialogue.message",
            arguments=MessageArguments(text="invalidate " + reason),
        )
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                current = await self.get(scope, session_id)
                if current is None or current.state in ("closed", "invalidated"):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if current.version != expected_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                revised = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_at": self.clock(),
                        "state": "invalidated",
                        "invalidated_reason": reason,
                        "generation": current.generation + 1,
                        "waiting_ticket_ids": [],
                        "checkpoint": None,
                        "pending_reply": None,
                        "context_sources": [],
                        "context_files": [],
                    }
                )
                await unit.put(revised, expected_version=current.version)
                receipt = Receipt(
                    request_id=request_id,
                    scope=scope,
                    status="completed",
                    resource_id=current.id,
                    version=revised.version,
                )
                await unit.complete(receipt)
                return receipt

    async def entries(
        self, scope: Scope, session_id: str, *, after_sequence: int = 0, limit: int = 50
    ) -> list[tuple[int, WorkSessionMessage | None]]:
        """Like ``messages``, but a row hidden by its source's lifecycle is an explicit gap.

        ``None`` at a sequence: the row is not readable (its source was deleted or expired).
        It is never an end of history; the caller decides to drop or to stop on it.
        """
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not (1 <= limit <= 200)
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        session = await self.get(scope, session_id)
        if session is None or session.state == "invalidated":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        repo = self.store.repository(WorkSessionMessage, self.principal, area(scope))
        rows: list[tuple[int, WorkSessionMessage | None]] = []
        sequences = range(
            after_sequence + 1, min(session.last_sequence, after_sequence + limit) + 1
        )
        found = {
            row.id: row
            for row in await repo.get_many(
                [message_id(scope, session_id, sequence) for sequence in sequences]
            )
        }
        for sequence in sequences:
            row = found.get(message_id(scope, session_id, sequence))
            if row is not None and (row.session_id != session_id or row.sequence != sequence):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            rows.append((sequence, row))
        return rows

    async def messages(
        self, scope: Scope, session_id: str, *, after_sequence: int = 0, limit: int = 50
    ) -> list[WorkSessionMessage]:
        rows: list[WorkSessionMessage] = []
        for _, row in await self.entries(
            scope, session_id, after_sequence=after_sequence, limit=limit
        ):
            if row is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)  # a missing row is not end of history
            rows.append(row)
        return rows

    async def open(self, request: CreateCommand) -> Receipt:
        args = WorkSessionOpenArguments.model_validate(request.arguments.data)
        if (
            request.type != "work.session.open"
            or request.target != "work"
            or (request.arguments.schema_id != "work.session.open-args")
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        scope = area(request.scope)
        identifier = derived_id("work-session", scope, request.request_id)
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                rows = await self.store.repository(WorkSession, self.principal, scope).list(
                    limit=self.max_sessions
                )
                # Bound the entire catalogue, including closed sessions. Retention is explicit.
                if len(rows) >= self.max_sessions:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                if args.task_id is not None:
                    from ai_agent_contracts.agent_workplace import WorkTask

                    task = await self.store.repository(WorkTask, self.principal, scope).get(
                        args.task_id
                    )
                    if task is None:
                        raise ContractError(ErrorCode.ACCESS_DENIED)
                now = self.clock()
                session = WorkSession(
                    id=identifier,
                    scope=scope,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    title=args.title,
                    task_ids=[] if args.task_id is None else [args.task_id],
                )
                await unit.put(session, access=self.readers or None)
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=identifier,
                    version=1,
                )
                await unit.complete(receipt)
                return receipt

    async def append(self, request: EditCommand, source: SourceRef) -> Receipt:
        """Store a validated operator message reference, never client-supplied sequence/role.

        The source owner must have ingested exactly this request's text with a
        stable external id; composition owns that binding. A replay reuses the
        committed receipt, while any changed request payload conflicts.
        """
        args = WorkSessionMessageArguments.model_validate(request.arguments.data)
        if (
            request.type != "work.session.message"
            or request.target != args.session_id
            or (request.arguments.schema_id != "work.session.message-args")
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        scope = area(request.scope)
        if source.scope.partition != scope.partition or source.availability != "available":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                session = await self.get(scope, args.session_id)
                if session is None or session.state in ("closed", "invalidated"):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if session.version != request.expected_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                if session.last_sequence >= self.max_messages:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                await self.verify_source(scope, source, args.text)
                now = self.clock()
                sequence = session.last_sequence + 1
                row = WorkSessionMessage(
                    id=message_id(scope, session.id, sequence),
                    scope=scope,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    session_id=session.id,
                    sequence=sequence,
                    speaker="operator",
                    source=source,
                )
                revised = session.model_copy(
                    update={
                        "version": session.version + 1,
                        "updated_at": now,
                        "last_sequence": sequence,
                        # New operator input supersedes an undelivered draft.
                        "pending_reply": None,
                    }
                )
                await unit.put(revised, expected_version=session.version)
                await unit.put(row, access=self.readers or None)
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=session.id,
                    version=revised.version,
                )
                await unit.complete(receipt)
                return receipt

    async def queue_reply(
        self,
        scope: Scope,
        request_id: str,
        session_id: str,
        *,
        expected_version: int,
        reply: SessionReplyRef,
        sources: tuple[SourceRef, ...],
        files: tuple[ArtifactRef, ...],
        tickets: tuple[SessionTicketRef, ...] = (),
    ) -> Receipt:
        """Runtime-only pointer to a gateway draft. No body and no consumed input yet."""
        scope = area(scope)
        payload = {
            "reply": reply.model_dump(mode="json"),
            "expected": expected_version,
            "sources": [r.model_dump(mode="json") for r in sources],
            "files": [r.model_dump(mode="json") for r in files],
            "tickets": [r.model_dump(mode="json") for r in tickets],
        }
        command = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=session_id,
            type="dialogue.message",
            arguments=MessageArguments(
                text=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
        )
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, command) as unit:
                if unit.replay is not None:
                    return unit.replay
                session = await self.get(scope, session_id)
                if session is None or session.state in ("closed", "invalidated"):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if session.version != expected_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                if session.pending_reply is not None:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                linked = list(
                    dict.fromkeys([*session.waiting_ticket_ids, *(t.ticket_id for t in tickets)])
                )
                if len(linked) > 20:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                revised = WorkSession.model_validate(
                    dict(
                        session.model_dump(),
                        version=session.version + 1,
                        updated_at=self.clock(),
                        pending_reply=reply,
                        context_sources=list(sources),
                        context_files=list(files),
                        checkpoint=None,
                        waiting_ticket_ids=linked,
                    )
                )
                await unit.put(revised, expected_version=session.version)
                receipt = Receipt(
                    request_id=request_id,
                    scope=scope,
                    status="completed",
                    resource_id=session.id,
                    version=revised.version,
                )
                await unit.complete(receipt)
                return receipt

    async def record_delivered(
        self,
        scope: Scope,
        request_id: str,
        session_id: str,
        *,
        generation: int,
        through_sequence: int,
        source: SourceRef,
        text: str,
        delivery_id: str,
        context_sources: tuple[SourceRef, ...],
        context_files: tuple[ArtifactRef, ...] = (),
        tickets: tuple[SessionTicketRef, ...] = (),
    ) -> Receipt:
        """Trusted delivery hook; no model/runner/client can assert that it was delivered.

        Source content is owned by memory. Only after the delivery owner verifies
        delivery do we append a character reference and advance the consumed prefix.
        Concurrent later operator input is preserved and remains pending.
        Rejected intake must be removed by the source owner, just like R3 operator intake.
        """
        if self.verify_delivery is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        scope = area(scope)
        if (
            type(through_sequence) is not int
            or through_sequence < 0
            or type(generation) is not int
            or generation < 1
            or not delivery_id
            or source.scope.partition != scope.partition
            or source.availability != "available"
            or len(context_sources) > 200
            or len(context_files) > 200
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        payload = {
            "generation": generation,
            "through": through_sequence,
            "source": source.model_dump(mode="json"),
            "delivery": delivery_id,
            "text_digest": hashlib.sha256(text.encode()).hexdigest(),
            "sources": [ref.model_dump(mode="json") for ref in context_sources],
            "files": [ref.model_dump(mode="json") for ref in context_files],
            "tickets": [ref.model_dump(mode="json") for ref in tickets],
        }
        command = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=session_id,
            type="dialogue.message",
            arguments=MessageArguments(
                text=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
        )
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, command) as unit:
                if unit.replay is not None:
                    return unit.replay
                session = await self.get(scope, session_id)
                if session is None or session.state in ("closed", "invalidated"):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if (
                    session.generation != generation
                    or not session.processed_sequence <= through_sequence <= session.last_sequence
                ):
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                if session.last_sequence >= self.max_messages:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                if through_sequence - session.processed_sequence > 200:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                prefix = [
                    row
                    for row in await self.messages(
                        scope, session_id, after_sequence=session.processed_sequence, limit=200
                    )
                    if row.sequence <= through_sequence
                ]
                if any(row.source not in context_sources for row in prefix):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                await self.verify_source(scope, source, text)
                await self.verify_delivery(scope, delivery_id, source)
                now = self.clock()
                sequence = session.last_sequence + 1
                row = WorkSessionMessage(
                    id=message_id(scope, session.id, sequence),
                    scope=scope,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    session_id=session.id,
                    sequence=sequence,
                    speaker="character",
                    source=source,
                    reply_to_sequence=max(
                        (item.sequence for item in prefix if item.speaker in INPUTS),
                        default=None,
                    ),
                )
                # Include our own appended reply only when there are no later unconsumed inputs.
                consumed = (
                    sequence if session.last_sequence == through_sequence else through_sequence
                )
                revisions = {ref.ticket_id: ref for ref in session.ticket_revisions}
                for ref in tickets:
                    if ref.ticket_id not in session.waiting_ticket_ids:
                        raise ContractError(ErrorCode.ACCESS_DENIED)
                    if (
                        ref.ticket_id not in revisions
                        or revisions[ref.ticket_id].version < ref.version
                    ):
                        revisions[ref.ticket_id] = ref
                revised = WorkSession.model_validate(
                    dict(
                        session.model_dump(),
                        version=session.version + 1,
                        updated_at=now,
                        last_sequence=sequence,
                        processed_sequence=consumed,
                        context_sources=list(context_sources),
                        context_files=list(context_files),
                        checkpoint=None,
                        pending_reply=None,
                        ticket_revisions=list(revisions.values()),
                    )
                )
                await unit.put(revised, expected_version=session.version)
                await unit.put(row, access=self.readers or None)
                receipt = Receipt(
                    request_id=request_id,
                    scope=scope,
                    status="completed",
                    resource_id=session.id,
                    version=revised.version,
                )
                await unit.complete(receipt)
                return receipt

    def _internal(
        self, request_id: str, scope: Scope, target: str, payload: object
    ) -> MessageCommand:
        """An idempotency key for an internal write: same request id, same exact payload."""
        return MessageCommand(
            request_id=request_id,
            scope=scope,
            target=target,
            type="dialogue.message",
            arguments=MessageArguments(
                text=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            ),
        )

    async def sessions(self, scope: Scope) -> list[WorkSession]:
        return await self.store.repository(WorkSession, self.principal, area(scope)).list(
            limit=self.max_sessions
        )

    async def timers(self, scope: Scope) -> list[WakeTimer]:
        rows = await self.store.repository(WakeTimer, self.principal, area(scope)).list(limit=200)
        return sorted(rows, key=lambda row: (row.due_at, row.id))

    async def set_timer(
        self,
        scope: Scope,
        request_id: str,
        session_id: str,
        minutes: int,
        reason: str,
        done_marker: str | None = None,
    ) -> WakeTimer:
        """The agent's own wake-up (W7): bounded in number, owned here, no right granted.

        The due time is taken from this owner's clock at the first commit; a replay of the
        same request returns that timer.
        """
        scope = area(scope)
        identifier = derived_id("wake-timer", scope, request_id)
        payload = {"session": session_id, "minutes": minutes, "reason": reason}
        if done_marker is not None:
            payload["done"] = done_marker
        command = self._internal(request_id, scope, session_id, payload)
        repo = self.store.repository(WakeTimer, self.principal, scope)
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, command) as unit:
                if unit.replay is None:
                    session = await self.get(scope, session_id)
                    if session is None or session.state in ("closed", "invalidated"):
                        raise ContractError(ErrorCode.ACCESS_DENIED)
                    pending = [t for t in await repo.list(limit=200) if t.state == "pending"]
                    if len(pending) >= MAX_PENDING_TIMERS:
                        raise ContractError(ErrorCode.RESOURCE_BUSY)
                    now = self.clock()
                    timer = WakeTimer(
                        id=identifier,
                        scope=scope,
                        version=1,
                        created_at=now,
                        updated_at=now,
                        session_id=session_id,
                        due_at=now + timedelta(minutes=minutes),
                        reason=reason,
                        done_marker=done_marker,
                    )
                    await unit.put(timer, access=self.readers or None)
                    await unit.complete(
                        Receipt(
                            request_id=request_id,
                            scope=scope,
                            status="completed",
                            resource_id=identifier,
                            version=1,
                        )
                    )
        found = await repo.get(identifier)
        if found is None:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return found

    async def wake(self, scope: Scope, timer: WakeTimer, source: SourceRef, text: str) -> int:
        """Fire one due timer: append its ``wake`` message, mark it fired, in one transaction."""
        scope = area(scope)
        if source.scope.partition != scope.partition or source.availability != "available":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        command = self._internal(
            "wake-" + timer.id, scope, timer.session_id, source.model_dump(mode="json")
        )
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, command) as unit:
                current = await self.store.repository(WakeTimer, self.principal, scope).get(
                    timer.id
                )
                if unit.replay is not None:
                    return 0 if current is None else current.fired_sequence or 0
                if current is None or current.state != "pending":
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                session = await self.get(scope, current.session_id)
                now = self.clock()
                if session is None or session.state in ("closed", "invalidated"):
                    # The operator closed the conversation: the wake-up is dropped, not retried.
                    dropped = current.model_copy(
                        update={"version": current.version + 1, "updated_at": now,
                                "state": "cancelled"}
                    )  # fmt: skip
                    await unit.put(dropped, expected_version=current.version)
                    await unit.complete(
                        Receipt(
                            request_id="wake-" + timer.id,
                            scope=scope,
                            status="completed",
                            resource_id=dropped.id,
                            version=dropped.version,
                        )
                    )
                    return 0
                if session.last_sequence >= self.max_messages:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                await self.verify_source(scope, source, text)
                sequence = session.last_sequence + 1
                row = WorkSessionMessage(
                    id=message_id(scope, session.id, sequence),
                    scope=scope,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    session_id=session.id,
                    sequence=sequence,
                    speaker="wake",
                    source=source,
                )
                revised = session.model_copy(
                    update={
                        "version": session.version + 1,
                        "updated_at": now,
                        "last_sequence": sequence,
                        "pending_reply": None,
                    }
                )
                fired = current.model_copy(
                    update={
                        "version": current.version + 1,
                        "updated_at": now,
                        "state": "fired",
                        "fired_sequence": sequence,
                    }
                )
                await unit.put(revised, expected_version=session.version)
                await unit.put(row, access=self.readers or None)
                await unit.put(fired, expected_version=current.version)
                await unit.complete(
                    Receipt(
                        request_id="wake-" + timer.id,
                        scope=scope,
                        status="completed",
                        resource_id=fired.id,
                        version=fired.version,
                    )
                )
                return sequence

    async def rename(self, request: EditCommand) -> Receipt:
        """Operator metadata only; does not append input or wake the character."""
        args = WorkSessionRenameArguments.model_validate(request.arguments.data)
        if (
            request.type != "work.session.rename"
            or request.target != args.session_id
            or request.arguments.schema_id != "work.session.rename-args"
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        scope = area(request.scope)
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                session = await self.get(scope, args.session_id)
                if session is None:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if session.version != request.expected_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                # Changing the version of an executing turn would invalidate its final CAS.
                attempts = await ActivityBook(
                    self.store, self.clock, principal=self.principal
                ).session_attempts(scope, session.id)
                if any(
                    a.session_id == session.id and a.state in ("prepared", "submitted")
                    for a in attempts
                ):
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                revised = session.model_copy(
                    update={
                        "version": session.version + 1,
                        "updated_at": self.clock(),
                        "title": args.title.strip(),
                    }
                )
                await unit.put(revised, expected_version=session.version)
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=session.id,
                    version=revised.version,
                )
                await unit.complete(receipt)
                return receipt

    async def close(self, request: EditCommand) -> Receipt:
        args = WorkSessionCloseArguments.model_validate(request.arguments.data)
        if (
            request.type != "work.session.close"
            or request.target != args.session_id
            or (request.arguments.schema_id != "work.session.close-args")
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        scope = area(request.scope)
        with self.store.fence(lambda: self.authorize(scope), owner=Owner.ACTIVITY):
            async with self.store.transaction(self.principal, scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                session = await self.get(scope, args.session_id)
                if session is None:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                if session.version != request.expected_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                revised = session.model_copy(
                    update={
                        "version": session.version + 1,
                        "updated_at": self.clock(),
                        "state": "closed",
                        "waiting_ticket_ids": [],
                        "checkpoint": None,
                        "pending_reply": None,
                    }
                )
                await unit.put(revised, expected_version=session.version)
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=session.id,
                    version=revised.version,
                )
                await unit.complete(receipt)
                return receipt
