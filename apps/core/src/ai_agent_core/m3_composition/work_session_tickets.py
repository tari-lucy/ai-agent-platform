"""Bounded ticket wakeups; only acknowledged replies advance the session cursor."""

from typing import TYPE_CHECKING

from ai_agent_contracts.agent_sessions import SessionTicketRef, WorkSession

from .workplace_experience import ticket_release

if TYPE_CHECKING:
    from .service import Runtime


async def updates(rt: Runtime, session: WorkSession) -> list[SessionTicketRef]:
    if rt.tickets is None:
        return []
    seen = {ref.ticket_id: ref.version for ref in session.ticket_revisions}
    found = []
    for identifier in session.waiting_ticket_ids:
        ticket = await rt.tickets.get(session.scope, identifier)
        if ticket is None or not (
            ticket.next_turn == "agent" or ticket.state in ("resolved", "closed_unfulfilled")
        ):
            continue
        # Reading a ticket also changes its version. Only a new operator message wakes work.
        messages = await rt.tickets.messages(session.scope, identifier)
        answered = max((m.ticket_version for m in messages if m.author == "operator"), default=0)
        if answered <= seen.get(identifier, 0):
            continue
        if await ticket_release(rt).open(session.scope, ticket, "api_transfer"):
            found.append(SessionTicketRef(ticket_id=identifier, version=ticket.version))
    # WorkContext releases at most ten tickets. Never consume a revision omitted from context.
    return found[:10]
