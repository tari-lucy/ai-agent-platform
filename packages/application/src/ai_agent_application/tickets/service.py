"""Ticket owner (AW-01): durable operator conversations; it never grants rights or runs work.

Authorship comes from the calling path, never from payload: the operator path writes only
operator messages, the agent/service path only its own. Every write is one owner transaction
(ticket revision + message) keyed by request_id, so a repeated command replays its receipt.
"""

import builtins
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ai_agent_contracts.agent_workplace import (
    CapabilityRequest,
    Ticket,
    TicketKind,
    TicketMessage,
    TicketParty,
    TicketReadArguments,
    TicketReplyArguments,
)
from ai_agent_contracts.api import (
    CommandBase,
    EditCommand,
    MessageArguments,
    MessageCommand,
    Receipt,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.storage import Owner, Principal, RecordAccess, Storage

MAX_LINKS = 50
TERMINAL = frozenset({"resolved", "closed_unfulfilled"})


@dataclass(frozen=True)
class TicketDraft:
    """What the agent or a platform service asks; ``basis`` merges repeats of one question."""

    kind: TicketKind
    title: str
    summary: str
    attempted: str
    decision_needed: str
    basis: str
    agent_priority: int = 1
    technical_refusal: bool = False
    task_ids: tuple[str, ...] = ()
    materials: tuple[ArtifactRef, ...] = field(default_factory=tuple)
    sources: tuple[SourceRef, ...] = field(default_factory=tuple)
    request: CapabilityRequest | None = None


def area(scope: Scope) -> Scope:
    """Tickets belong to the stable partition, not to one run or session."""
    return Scope(
        workspace_id=scope.workspace_id,
        persona_id=scope.persona_id,
        memory_space_id=scope.memory_space_id,
        memory_mode=scope.memory_mode,
    )


def duplicate_key(kind: str, basis: str) -> str:
    normal = " ".join(basis.casefold().split())
    return hashlib.sha256(json.dumps([kind, normal]).encode()).hexdigest()


def ticket_id(key: str) -> str:
    return "ticket-" + key[:32]


def message_id(ticket: str, sequence: int) -> str:
    return f"{ticket}.m{sequence:05d}"


def _merge[T](old: Sequence[T], new: Sequence[T], key: Callable[[T], object]) -> list[T]:
    seen: dict[object, T] = {key(item): item for item in old}
    for item in new:
        seen.setdefault(key(item), item)
    return list(seen.values())[-MAX_LINKS:]


class TicketService:
    def __init__(
        self,
        store: Storage,
        clock: Callable[[], datetime],
        readers: tuple[RecordAccess, ...] = (),
        principal: Principal | None = None,
    ) -> None:
        self.store, self.clock, self.readers = store, clock, readers
        self.principal = principal or Principal("core-tickets", "core_owner", Owner.TICKETS)

    async def get(self, scope: Scope, identifier: str) -> Ticket | None:
        return await self.store.repository(Ticket, self.principal, area(scope)).get(identifier)

    async def messages(self, scope: Scope, identifier: str) -> builtins.list[TicketMessage]:
        ticket = await self.get(scope, identifier)
        if ticket is None:
            return []
        repo = self.store.repository(TicketMessage, self.principal, area(scope))
        count = ticket.message_count
        rows = [await repo.get(message_id(identifier, n)) for n in range(1, count + 1)]
        return [row for row in rows if row is not None]

    async def pending_for_agent(self, scope: Scope, *, limit: int = 20) -> builtins.list[Ticket]:
        rows: builtins.list[Ticket] = []
        cursor: str | None = None
        repo = self.store.repository(Ticket, self.principal, area(scope))
        while len(rows) < limit:
            page = await repo.list(limit=200, after_id=cursor)
            rows += [row for row in page if row.next_turn == "agent"]
            if len(page) < 200:
                break
            cursor = page[-1].id
        return rows[:limit]

    async def _commit(
        self,
        command: CommandBase,
        identifier: str,
        build: Callable[[Ticket | None], tuple[Ticket, TicketMessage | None]],
    ) -> Receipt:
        """Replay first; then read, build and CAS-write the revision in one owner transaction."""
        scope = area(command.scope)
        async with self.store.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            current = await self.get(scope, identifier)
            ticket, message = build(current)
            expected = None if current is None else current.version
            await unit.put(ticket, expected_version=expected, access=self.readers or None)
            if message is not None:
                await unit.put(message, access=self.readers or None)
            receipt = Receipt(
                request_id=command.request_id,
                scope=command.scope,
                status="completed",
                resource_id=ticket.id,
                version=ticket.version,
            )
            await unit.complete(receipt)
            return receipt

    def _message(
        self, ticket: Ticket, author: TicketParty, author_id: str, body: str
    ) -> TicketMessage:
        now = self.clock()
        return TicketMessage(
            id=message_id(ticket.id, ticket.message_count),
            scope=ticket.scope,
            created_at=now,
            ticket_id=ticket.id,
            sequence=ticket.message_count,
            author=author,
            author_id=author_id,
            body=body,
            ticket_version=ticket.version,
        )

    async def raise_ticket(
        self,
        request_id: str,
        scope: Scope,
        author: Literal["agent", "service"],
        author_id: str,
        draft: TicketDraft,
        body: str,
    ) -> Receipt:
        """Open a ticket, or append to (and reopen) the one with the same kind and basis."""
        scope = area(scope)
        key = duplicate_key(draft.kind, draft.basis)
        identifier = ticket_id(key)
        payload = json.dumps(
            [author, author_id, draft.kind, draft.title, draft.summary, body], ensure_ascii=False
        )
        command = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=identifier,
            type="dialogue.message",
            arguments=MessageArguments(text=hashlib.sha256(payload.encode()).hexdigest()),
        )

        def build(current: Ticket | None) -> tuple[Ticket, TicketMessage | None]:
            now = self.clock()
            if current is None:
                ticket = Ticket(
                    id=identifier,
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    ticket_id=identifier,
                    kind=draft.kind,
                    title=draft.title,
                    summary=draft.summary,
                    attempted=draft.attempted,
                    decision_needed=draft.decision_needed,
                    state="waiting_reply",
                    next_turn="operator",
                    task_ids=list(draft.task_ids),
                    materials=list(draft.materials),
                    sources=list(draft.sources),
                    agent_priority=draft.agent_priority,
                    technical_refusal=draft.technical_refusal,
                    duplicate_key=key,
                    message_count=1,
                    operator_read_version=0,
                    request=draft.request,
                )
            else:
                ticket = _revise(
                    current,
                    now,
                    summary=draft.summary,
                    attempted=draft.attempted,
                    decision_needed=draft.decision_needed,
                    state="waiting_reply",
                    next_turn="operator",
                    resolution=None,
                    agent_priority=max(current.agent_priority, draft.agent_priority),
                    technical_refusal=current.technical_refusal or draft.technical_refusal,
                    task_ids=_merge(current.task_ids, draft.task_ids, str)[-20:],
                    materials=_merge(
                        current.materials, draft.materials, lambda r: (r.artifact_id, r.version)
                    ),
                    sources=_merge(current.sources, draft.sources, _source_key),
                    occurrences=current.occurrences + 1,
                    message_count=current.message_count + 1,
                    request=draft.request or current.request,
                )
            return ticket, self._message(ticket, author, author_id, body)

        return await self._commit(command, identifier, build)

    async def agent_update(
        self,
        request_id: str,
        scope: Scope,
        author_id: str,
        identifier: str,
        body: str,
        outcome: Literal["message", "resolve"],
        evidence: Sequence[SourceRef] = (),
    ) -> Receipt:
        """The agent answers or records a recovery; resolve needs evidence or an operator answer."""
        scope = area(scope)
        current = await self.get(scope, identifier)
        if current is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
        if outcome == "resolve" and not evidence:
            # Without evidence only an answered question may be closed: the operator's own
            # last message is its basis. A capability request needs proof of readiness.
            messages = await self.messages(scope, identifier)
            if (
                current.kind == "capability_request"
                or not messages
                or messages[-1].author != "operator"
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
        command = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=identifier,
            type="dialogue.message",
            arguments=MessageArguments(
                text=hashlib.sha256(json.dumps([author_id, outcome, body]).encode()).hexdigest()
            ),
        )
        resolved = outcome == "resolve"

        def build(current: Ticket | None) -> tuple[Ticket, TicketMessage | None]:
            if current is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
            ticket = _revise(
                current,
                self.clock(),
                state="resolved" if resolved else "waiting_reply",
                next_turn=None if resolved else "operator",
                resolution=body if resolved else None,
                sources=_merge(current.sources, evidence, _source_key),
                message_count=current.message_count + 1,
            )
            return ticket, self._message(ticket, "agent", author_id, body)

        return await self._commit(command, identifier, build)

    async def note(self, request_id: str, scope: Scope, identifier: str, body: str) -> Receipt:
        """A platform-service record on a ticket (e.g. a decision receipt); turn is unchanged."""
        command = MessageCommand(
            request_id=request_id,
            scope=area(scope),
            target=identifier,
            type="dialogue.message",
            arguments=MessageArguments(text=hashlib.sha256(body.encode()).hexdigest()),
        )

        def build(current: Ticket | None) -> tuple[Ticket, TicketMessage | None]:
            if current is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
            ticket = _revise(current, self.clock(), message_count=current.message_count + 1)
            return ticket, self._message(ticket, "service", "platform", body)

        return await self._commit(command, identifier, build)

    async def reply(
        self, operator_id: str, request: EditCommand, args: TicketReplyArguments
    ) -> Receipt:
        """Operator answer on the exact seen version; allowed while the agent is STOPPED."""
        state, turn = {
            "answer": ("in_progress", "agent"),
            "resolve": ("resolved", None),
            "close": ("closed_unfulfilled", None),
        }[args.outcome]

        def build(current: Ticket | None) -> tuple[Ticket, TicketMessage | None]:
            current = _seen(request, current)
            ticket = _revise(
                current,
                self.clock(),
                state=state,
                next_turn=turn,
                resolution=None if turn else args.body,
                message_count=current.message_count + 1,
                operator_read_version=current.version + 1,
            )
            return ticket, self._message(ticket, "operator", operator_id, args.body)

        _target(request, args.ticket_id)
        return await self._commit(request, args.ticket_id, build)

    async def mark_read(self, request: EditCommand, args: TicketReadArguments) -> Receipt:
        def build(current: Ticket | None) -> tuple[Ticket, TicketMessage | None]:
            current = _seen(request, current)
            if current.operator_read_version == current.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT, request.request_id, current.version)
            return _revise(current, self.clock(), operator_read_version=current.version + 1), None

        _target(request, args.ticket_id)
        return await self._commit(request, args.ticket_id, build)


def _source_key(ref: SourceRef) -> tuple[str, int]:
    return ref.source_id, ref.version


def _revise(current: Ticket, now: datetime, **changes: object) -> Ticket:
    data = current.model_dump()
    data.update(version=current.version + 1, updated_at=now, **changes)
    return Ticket.model_validate(data)


def _target(request: EditCommand, identifier: str) -> None:
    if request.target != identifier or request.scope != area(request.scope):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS, request.request_id)


def _seen(request: EditCommand, current: Ticket | None) -> Ticket:
    if current is None:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS, request.request_id)
    if current.version != request.expected_version:
        raise ContractError(ErrorCode.VERSION_CONFLICT, request.request_id, current.version)
    return current
