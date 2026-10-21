"""Activity-owned logical sessions. No runner, model call, rights grant or auto-start.

Trusted composition stores text through the existing Source owner BEFORE append.
Only references are stored here. R2 releases bodies/history through their owners.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from ai_agent_contracts.agent_sessions import (
    WorkSession,
    WorkSessionCloseArguments,
    WorkSessionMessage,
    WorkSessionMessageArguments,
    WorkSessionOpenArguments,
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

from .book import area


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

    async def messages(
        self, scope: Scope, session_id: str, *, after_sequence: int = 0, limit: int = 50
    ) -> list[WorkSessionMessage]:
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
        rows: list[WorkSessionMessage] = []
        for sequence in range(
            after_sequence + 1, min(session.last_sequence, after_sequence + limit) + 1
        ):
            row = await repo.get(message_id(scope, session_id, sequence))
            if row is None or row.session_id != session_id or row.sequence != sequence:
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
                        (item.sequence for item in prefix if item.speaker == "operator"),
                        default=None,
                    ),
                )
                # Include our own appended reply only when there are no later unconsumed inputs.
                consumed = (
                    sequence if session.last_sequence == through_sequence else through_sequence
                )
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
