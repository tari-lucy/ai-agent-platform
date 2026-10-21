"""Operator reads of tickets through their release gate (R2).

Wraps the generic reader: ``tickets`` and ``ticket-messages`` pages (and single records) leave
the core only after ``TicketRelease`` for ``api_transfer`` (the panel transfer). Every other
resource passes through unchanged. ACL, TTL and paging stay the generic reader's.
"""

from typing import TYPE_CHECKING, Any

from ai_agent_application.storage import ReadPurpose
from ai_agent_application.tickets.release import WITHHELD
from ai_agent_contracts.agent_workplace import Ticket, TicketMessage
from ai_agent_contracts.api import Page
from ai_agent_contracts.common import Contract, Scope

from .workplace_experience import ticket_release

if TYPE_CHECKING:
    from ai_agent_core.access.surface import Reader

    from .service import Runtime

PURPOSE: ReadPurpose = "api_transfer"


class ReleasedReader:
    def __init__(self, reader: Reader, rt: Runtime) -> None:
        self.reader, self.rt = reader, rt

    async def read(
        self,
        resource: str,
        principal_id: str,
        scope: Scope,
        resource_id: str | None,
        limit: int,
        cursor: str | None,
        order: str | None = None,
        utterance_id: str | None = None,
    ) -> Contract | None:
        kwargs: dict[str, Any] = {}
        if order is not None or utterance_id is not None:
            kwargs = {"order": order, "utterance_id": utterance_id}
        model = await self.reader.read(
            resource, principal_id, scope, resource_id, limit, cursor, **kwargs
        )
        if model is None or resource not in ("tickets", "ticket-messages"):
            return model
        release = ticket_release(self.rt)
        if resource == "tickets":
            if isinstance(model, Ticket):
                return await release.ticket(scope, model, PURPOSE)
            assert isinstance(model, Page)
            items = [await release.ticket(scope, row, PURPOSE) for row in model.items]
            return model.model_copy(update={"items": items})
        rows: list[TicketMessage] = (
            [model] if isinstance(model, TicketMessage) else list(model.items)  # type: ignore[attr-defined]
        )
        # One release decision per ticket on the page, not per message; None = no ticket.
        decided: dict[str, bool | None] = {}
        released: list[TicketMessage] = []
        for row in rows:
            if row.ticket_id not in decided:
                tickets = self.rt.tickets
                ticket = None if tickets is None else await tickets.get(scope, row.ticket_id)
                decided[row.ticket_id] = (
                    None if ticket is None else await release.open(scope, ticket, PURPOSE)
                )
            is_open = decided[row.ticket_id]
            if is_open is None:
                continue  # a message without its ticket is not shown
            released.append(
                row
                if is_open or row.author == "operator"
                else row.model_copy(update={"body": WITHHELD})
            )
        if isinstance(model, TicketMessage):
            return released[0] if released else None
        return model.model_copy(update={"items": released})
