"""Release of ticket history (R2): what of a ticket may leave its owner for a given purpose.

A ticket's text (title, summary, attempted, decision, resolution, request constraints) and the
agent's/services' messages are derived from its recorded bases: ``sources`` and ``materials``.
They are released only while every basis is still releasable for the purpose: the source is
available, at the recorded version, allowed for that purpose and not blocked in the deletion
ledger; each material passes the file owner's guard. The text is not separable per basis, so
one closed basis withholds all derived text (a safe first variant). The operator's own messages
are the operator's words and stay. Ids, kinds, states and turns stay so the operator can still
see that a ticket exists and answer or close it. Nothing here deletes data: physical purge is
the retention owner's; this only gates what is shown to the panel or a model.
"""

import asyncio
from collections.abc import Sequence
from typing import Protocol

from ai_agent_contracts.agent_workplace import Ticket, TicketMessage
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.retention.ports import DeletionLedger
from ai_agent_application.storage import ReadPurpose

WITHHELD = "Скрыто: основание этого обращения отозвано, истекло или удалено."
CLOSED_FILE = {ErrorCode.ACCESS_DENIED, ErrorCode.EXPIRED, ErrorCode.STALE_CONTEXT}


class SourceRights(Protocol):
    """The memory owner's current record of a source (any purpose filter is applied here)."""

    async def get_source(self, scope: Scope, source_id: str) -> Source | None: ...


class FileGuard(Protocol):
    """The file owner's release guard (``FileReleaseGuard.check``)."""

    async def check(
        self,
        scope: Scope,
        sources: Sequence[SourceRef],
        files: Sequence[ArtifactRef],
        purpose: ReadPurpose = "private",
    ) -> None: ...


class TicketRelease:
    def __init__(
        self,
        sources: SourceRights,
        *,
        ledger: DeletionLedger | None = None,
        files: FileGuard | None = None,
    ) -> None:
        self.sources, self.ledger, self.files = sources, ledger, files

    async def bases_open(
        self,
        scope: Scope,
        sources: Sequence[SourceRef],
        materials: Sequence[ArtifactRef],
        purpose: ReadPurpose,
    ) -> bool:
        """Every given basis is still releasable for ``purpose`` (also for session checkpoints)."""
        blocked: set[str] = set()
        if self.ledger is not None and sources:
            blocked = {entry.source_id for entry in await self.ledger.entries(scope)}
        if any(
            ref.source_id in blocked or ref.scope.partition != scope.partition for ref in sources
        ):
            return False
        # Bounded independent reads. Every invocation asks the owner again: no
        # cached source/permission, unbounded task fan-out or shortened history.
        for start in range(0, len(sources), 4):
            refs = sources[start : start + 4]
            rows = await asyncio.gather(
                *(self.sources.get_source(scope, ref.source_id) for ref in refs)
            )
            for ref, source in zip(refs, rows, strict=True):
                if (
                    source is None
                    or source.availability != "available"
                    or source.version != ref.version
                    or not source.read_allowed
                    or (purpose == "api_transfer" and not source.api_transfer_allowed)
                    or (purpose == "public_disclosure" and not source.public_disclosure_allowed)
                ):
                    return False
        if materials:
            if self.files is None:
                return False  # a material that cannot be checked is not released
            try:
                await self.files.check(scope, (), materials, purpose)
            except ContractError as error:
                if error.error.code in CLOSED_FILE:
                    return False
                raise
        return True

    async def open(self, scope: Scope, ticket: Ticket, purpose: ReadPurpose) -> bool:
        """Every basis of the ticket's text is still releasable for ``purpose``."""
        return await self.bases_open(scope, ticket.sources, ticket.materials, purpose)

    async def ticket(self, scope: Scope, ticket: Ticket, purpose: ReadPurpose) -> Ticket:
        if await self.open(scope, ticket, purpose):
            return ticket
        return withhold(ticket)

    async def messages(
        self,
        scope: Scope,
        ticket: Ticket,
        rows: Sequence[TicketMessage],
        purpose: ReadPurpose,
    ) -> list[TicketMessage]:
        if await self.open(scope, ticket, purpose):
            return list(rows)
        return [
            row if row.author == "operator" else row.model_copy(update={"body": WITHHELD})
            for row in rows
        ]


def withhold(ticket: Ticket) -> Ticket:
    """The same ticket with every derived text replaced; refs/ids/state stay for control."""
    request = ticket.request
    return Ticket.model_validate(
        ticket.model_dump()
        | {
            "title": "Обращение скрыто",
            "summary": WITHHELD,
            "attempted": WITHHELD,
            "decision_needed": WITHHELD,
            "resolution": None if ticket.resolution is None else WITHHELD,
            "request": None
            if request is None
            else request.model_copy(update={"constraints": WITHHELD}).model_dump(),
        }
    )
