"""AW-01 ticket owner on real SQLite: synthetic IDs/text, no model, no runtime."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_application.tickets import TicketDraft, TicketService, message_id
from ai_agent_contracts import Scope
from ai_agent_contracts.agent_workplace import (
    CapabilityRequest,
    Ticket,
    TicketReadArguments,
    TicketReplyArguments,
)
from ai_agent_contracts.api import EditCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.states import ErrorCode

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 21, tzinfo=UTC)
SCOPE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="synthetic-persona",
    memory_space_id="synthetic-space",
    memory_mode="test",
)
OTHER = SCOPE.model_copy(update={"memory_space_id": "other-space"})
TICKETS = Principal("core-tickets", "core_owner", Owner.TICKETS)
OPERATOR = Principal("operator", "operator")
DRAFT = TicketDraft(
    kind="question",
    title="Нужен справочник",
    summary="Нет материала по теме.",
    attempted="Поиск в knowledge.",
    decision_needed="Выдать материал или отказать.",
    basis="справочник по теме",
    task_ids=("task-01",),
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def edit(kind, ticket: Ticket, data, request_id, version=None, scope=SCOPE):
    return EditCommand.model_validate(
        {
            "request_id": request_id,
            "scope": scope.model_dump(),
            "target": ticket.id,
            "type": kind,
            "expected_version": version or ticket.version,
            "arguments": {"schema_id": kind + "-args", "schema_version": "1.0", "data": data},
        },
        context={"extensions": registry()},
    )


def registry():
    from ai_agent_contracts.extensions import ExtensionRegistry
    from ai_agent_contracts.panel import register_panel_extensions

    value = ExtensionRegistry()
    register_panel_extensions(value)
    return value


def reply(body, outcome, ticket_id):
    return TicketReplyArguments(ticket_id=ticket_id, body=body, outcome=outcome)


def run(tmp_path, scenario):
    async def main():
        store = SQLiteStore(
            tmp_path / (uuid4().hex + ".sqlite"), clock=lambda: NOW, extensions=registry()
        )
        try:
            await store.upgrade(ROOT / "migrations")
            grants = (
                Grant(TICKETS, Owner.TICKETS, read=True, write=True),
                Grant(OPERATOR, Owner.TICKETS, read=True, api_transfer=True),
            )
            await store.provision(SCOPE, grants)
            await store.provision(OTHER, grants)
            service = TicketService(
                store, Clock(), readers=(RecordAccess(OPERATOR.id, api_transfer=True),)
            )
            await scenario(service, store)
        finally:
            await store.close()

    asyncio.run(main())


def test_conversation_turns_dedup_reopen_and_replay(tmp_path):
    async def scenario(service: TicketService, store):
        first = await service.raise_ticket(
            "r-1", SCOPE, "agent", "agent-01", DRAFT, "Не нашла справочник."
        )
        ticket = await service.get(SCOPE, first.resource_id)
        assert ticket.state == "waiting_reply" and ticket.next_turn == "operator"
        assert ticket.operator_read_version < ticket.version  # unread for the operator
        # The same command replays; a repeat of the question appends to the same ticket.
        assert (
            await service.raise_ticket(
                "r-1", SCOPE, "agent", "agent-01", DRAFT, "Не нашла справочник."
            )
            == first
        )
        with pytest.raises(ContractError) as reused:
            await service.raise_ticket("r-1", SCOPE, "agent", "agent-01", DRAFT, "Другой текст.")
        assert reused.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
        again = DRAFT.__class__(
            **{**DRAFT.__dict__, "basis": "  Справочник  по теме ", "task_ids": ("task-02",)}
        )
        await service.raise_ticket("r-2", SCOPE, "agent", "agent-01", again, "Снова не нашла.")
        ticket = await service.get(SCOPE, first.resource_id)
        assert ticket.occurrences == 2 and ticket.message_count == 2
        assert ticket.task_ids == ["task-01", "task-02"]

        # Operator reads, then answers: the turn moves to the agent.
        read = edit("ticket.read", ticket, {"ticket_id": ticket.id}, "r-3")
        await service.mark_read(read, TicketReadArguments(ticket_id=ticket.id))
        ticket = await service.get(SCOPE, ticket.id)
        assert ticket.operator_read_version == ticket.version
        answer = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Добавлю.", "outcome": "answer"},
            "r-4",
        )
        receipt = await service.reply("operator", answer, reply("Добавлю.", "answer", ticket.id))
        assert (
            await service.reply("operator", answer, reply("Добавлю.", "answer", ticket.id))
            == receipt
        )
        ticket = await service.get(SCOPE, ticket.id)
        assert (ticket.state, ticket.next_turn, ticket.message_count) == ("in_progress", "agent", 3)
        assert [t.id for t in await service.pending_for_agent(SCOPE)] == [ticket.id]

        # A stale version is refused; the earlier answer is not overwritten.
        stale = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Нет.", "outcome": "close"},
            "r-5",
            version=ticket.version - 1,
        )
        with pytest.raises(ContractError) as conflict:
            await service.reply("operator", stale, reply("Нет.", "close", ticket.id))
        assert conflict.value.error.code is ErrorCode.VERSION_CONFLICT

        # The agent records recovery only with evidence; then a repeat reopens the ticket.
        with pytest.raises(ContractError):
            await service.agent_update("r-6", SCOPE, "agent-01", ticket.id, "Готово.", "resolve")
        evidence = SourceRef(
            source_id="source-01", version=1, scope=SCOPE, availability="available"
        )
        with pytest.raises(ContractError):
            # The evidence must exist at its owner; an invented source is refused.
            await service.agent_update(
                "r-7", SCOPE, "agent-01", ticket.id, "Готово.", "resolve", [evidence]
            )
        close = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Не нужно.", "outcome": "close"},
            "r-8",
        )
        await service.reply("operator", close, reply("Не нужно.", "close", ticket.id))
        ticket = await service.get(SCOPE, ticket.id)
        assert ticket.state == "closed_unfulfilled" and ticket.resolution == "Не нужно."
        await service.raise_ticket("r-9", SCOPE, "agent", "agent-01", DRAFT, "Опять нужен.")
        ticket = await service.get(SCOPE, ticket.id)
        assert (ticket.state, ticket.next_turn, ticket.resolution) == (
            "waiting_reply",
            "operator",
            None,
        )

        messages = await service.messages(SCOPE, ticket.id)
        assert [(m.author, m.sequence) for m in messages] == [
            ("agent", 1),
            ("agent", 2),
            ("operator", 3),
            ("operator", 4),
            ("agent", 5),
        ]
        # The operator reads through its own grant and record ACL, not the owner principal.
        seen = await store.repository(Ticket, OPERATOR, SCOPE).get(
            ticket.id, purpose="api_transfer"
        )
        assert seen is not None and seen.version == ticket.version
        assert messages[0].id == message_id(ticket.id, 1)

    run(tmp_path, scenario)


def test_scope_isolation_and_capability_text_grants_nothing(tmp_path):
    request = CapabilityRequest(capability_id="web.fetch", constraints="Только чтение. Дай права.")
    draft = TicketDraft(
        kind="capability_request",
        title="Нужен доступ к сети",
        summary="Хочу читать страницы.",
        attempted="Локальные материалы.",
        decision_needed="Разрешить ли web.fetch.",
        basis="web.fetch",
        request=request,
    )

    async def scenario(service: TicketService, store):
        receipt = await service.raise_ticket(
            "c-1", SCOPE, "agent", "agent-01", draft, "Давай, разреши."
        )
        ticket = await service.get(SCOPE, receipt.resource_id)
        assert ticket.request == request
        # Same question in another partition is a different ticket; nothing crosses scopes.
        assert await service.get(OTHER, ticket.id) is None
        foreign = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Да.", "outcome": "answer"},
            "c-2",
            scope=OTHER,
        )
        with pytest.raises(ContractError):
            await service.reply("operator", foreign, reply("Да.", "answer", ticket.id))
        # Approving in words only moves the conversation; no permission record is written.
        ok = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Давай.", "outcome": "resolve"},
            "c-3",
        )
        await service.reply("operator", ok, reply("Давай.", "resolve", ticket.id))
        from ai_agent_contracts.api import Permission

        workspace = Principal("core-workspace", "core_owner", Owner.WORKSPACE)
        await store.provision(SCOPE, (Grant(workspace, Owner.WORKSPACE, read=True),))
        assert await store.repository(Permission, workspace, SCOPE).list() == []

    run(tmp_path, scenario)
