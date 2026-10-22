"""Wake cursor uses operator answers, bounded release and acknowledged revisions."""

import asyncio
from types import SimpleNamespace

from ai_agent_contracts.agent_sessions import SessionReplyOrigin, SessionTicketRef, WorkSession
from test_sessions import AREA, NOW

from ai_agent_core.m3_composition import work_session_tickets as module


def test_read_metadata_does_not_wake_and_withheld_tickets_never_enter_ready_set(monkeypatch):
    async def scenario():
        ticket = SimpleNamespace(id="ticket", version=7, next_turn="agent", state="in_progress")
        message = SimpleNamespace(author="operator", ticket_version=3)
        allowed = True

        async def get(*_):
            return ticket

        async def messages(*_):
            return [message]

        async def release(*_):
            return allowed

        monkeypatch.setattr(module, "ticket_release", lambda _: SimpleNamespace(open=release))
        rt = SimpleNamespace(tickets=SimpleNamespace(get=get, messages=messages))
        session = WorkSession(
            id="session",
            scope=AREA,
            version=1,
            created_at=NOW,
            updated_at=NOW,
            title="Synthetic",
            waiting_ticket_ids=["ticket"],
            ticket_revisions=[SessionTicketRef(ticket_id="ticket", version=3)],
        )
        assert await module.updates(rt, session) == []  # Version 7 is only read metadata.
        message.ticket_version = 8
        ticket.version = 8
        assert await module.updates(rt, session) == [
            SessionTicketRef(ticket_id="ticket", version=8)
        ]
        allowed = False
        assert await module.updates(rt, session) == []
        allowed = True
        ticket.next_turn, ticket.state = None, "closed_unfulfilled"
        assert await module.updates(rt, session)  # An explicit refusal is information too.
        ticket.next_turn, ticket.state = "operator", "waiting_reply"
        assert await module.updates(rt, session) == []
        # Ticket-triggered replies do not invent an operator history entry.
        SessionReplyOrigin(
            session_id="session",
            generation=1,
            after_sequence=2,
            through_sequence=2,
            tickets=[SessionTicketRef(ticket_id="ticket", version=8)],
        )

    asyncio.run(scenario())
