"""Session context port (R2): what a working session of the person may be given, with its bases.

Independent of the session transport (R1): the session asks for a context, gets bounded text
plus the exact refs it was built from, keeps those refs with its checkpoint, and before
resuming asks ``still_valid``. If any basis was revoked, expired, deleted, blocked in the ledger
or moved to a new version, the checkpoint's context must be dropped and built again; the task
itself stays for a safe continuation. Nothing is stored here: no hidden long-term memory of the
harness. The refs are also the attempt's inputs, so whatever the session produces keeps them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef

from ai_agent_application.dialogue.context import data
from ai_agent_application.storage import ReadPurpose
from ai_agent_application.tickets import TicketRelease, TicketService

from .context import PersonaContext

PURPOSE: ReadPurpose = "api_transfer"  # everything here is meant for the model


@dataclass(frozen=True)
class ContextRefs:
    sources: tuple[SourceRef, ...] = ()
    files: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True)
class TicketView:
    """One ticket as the session may see it; ``withheld`` means its derived text is closed."""

    ticket_id: str
    kind: str
    state: str
    next_turn: str | None
    title: str
    messages: tuple[tuple[str, str], ...]  # (author, body), oldest first, bounded
    withheld: bool


@dataclass(frozen=True)
class SessionContext:
    persona: str
    tickets: tuple[TicketView, ...]
    refs: ContextRefs
    omitted: int  # memories/obligations/tickets left out for rights or bounds

    def text(self) -> str:
        """A plain rendering for a prompt; every value is data, not instructions."""
        lines = [self.persona] if self.persona else []
        for view in self.tickets:
            head = f"Обращение {view.ticket_id} ({view.kind}, {view.state}): {data(view.title)}"
            lines.append(head)
            lines += [f"  {author}: {data(body)}" for author, body in view.messages]
        return "\n".join(lines)


class SessionContextPort(Protocol):
    async def build(
        self, scope: Scope, query: str, ticket_ids: Sequence[str] = ()
    ) -> SessionContext: ...

    async def still_valid(self, scope: Scope, refs: ContextRefs) -> bool: ...


class WorkContext:
    """SessionContextPort over the persona, tickets and release owners."""

    def __init__(
        self,
        persona: PersonaContext,
        tickets: TicketService | None,
        release: TicketRelease,
        *,
        max_tickets: int = 10,
        max_messages: int = 12,
        max_body: int = 1500,
    ) -> None:
        self.persona, self.tickets, self.release = persona, tickets, release
        self.max_tickets, self.max_messages, self.max_body = max_tickets, max_messages, max_body

    async def build(
        self, scope: Scope, query: str, ticket_ids: Sequence[str] = ()
    ) -> SessionContext:
        brief = await self.persona.build(scope, query)
        sources = list(brief.sources)
        files: list[ArtifactRef] = []
        views: list[TicketView] = []
        omitted = brief.omitted
        wanted = list(dict.fromkeys(ticket_ids))
        omitted += max(0, len(wanted) - self.max_tickets)
        for ticket_id in wanted[: self.max_tickets] if self.tickets is not None else ():
            assert self.tickets is not None
            ticket = await self.tickets.get(scope, ticket_id)
            if ticket is None:
                omitted += 1
                continue
            open_ = await self.release.open(scope, ticket, PURPOSE)
            shown = ticket if open_ else await self.release.ticket(scope, ticket, PURPOSE)
            rows = await self.release.messages(
                scope, ticket, await self.tickets.messages(scope, ticket_id), PURPOSE
            )
            if open_:
                # Shown text depends on these bases: they travel with the context.
                sources += ticket.sources
                files += ticket.materials
            views.append(
                TicketView(
                    ticket_id=ticket.id,
                    kind=ticket.kind,
                    state=ticket.state,
                    next_turn=ticket.next_turn,
                    title=shown.title,
                    messages=tuple(
                        (row.author, row.body[: self.max_body])
                        for row in rows[-self.max_messages :]
                    ),
                    withheld=not open_,
                )
            )
        refs = ContextRefs(
            tuple({(r.source_id, r.version): r for r in sources}.values()),
            tuple({(r.artifact_id, r.version): r for r in files}.values()),
        )
        return SessionContext(brief.text, tuple(views), refs, omitted)

    async def still_valid(self, scope: Scope, refs: ContextRefs) -> bool:
        return await self.release.bases_open(scope, refs.sources, refs.files, PURPOSE)
