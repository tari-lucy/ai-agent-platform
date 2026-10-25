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
from collections.abc import Awaitable, Callable, Sequence
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
        bulk_sources: Callable[[Scope, Sequence[str]], Awaitable[list[Source]]] | None = None,
    ) -> None:
        self.sources, self.ledger, self.files = sources, ledger, files
        self.bulk_sources = bulk_sources

    async def source_statuses(
        self, scope: Scope, sources: Sequence[SourceRef], purpose: ReadPurpose
    ) -> tuple[bool, ...]:
        """Fresh per-reference release decisions, one bounded owner read; no text/cache."""
        if len(sources) > 200:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        blocked = (
            set()
            if self.ledger is None or not sources
            else {entry.source_id for entry in await self.ledger.entries(scope)}
        )
        wanted = [
            r
            for r in sources
            if r.scope.partition == scope.partition and r.source_id not in blocked
        ]
        if self.bulk_sources is not None and wanted:
            found = {s.id: s for s in await self.bulk_sources(scope, [r.source_id for r in wanted])}
        else:
            found = {}
            for start in range(0, len(wanted), 4):
                refs = wanted[start : start + 4]
                rows = await asyncio.gather(
                    *(self.sources.get_source(scope, r.source_id) for r in refs)
                )
                found.update((source.id, source) for source in rows if source is not None)
        return tuple(
            ref.scope.partition == scope.partition
            and ref.source_id not in blocked
            and (source := found.get(ref.source_id)) is not None
            and source.availability == "available"
            and source.version == ref.version
            and source.read_allowed
            and (purpose != "api_transfer" or source.api_transfer_allowed)
            and (purpose != "public_disclosure" or source.public_disclosure_allowed)
            for ref in sources
        )

    async def bases_open(
        self,
        scope: Scope,
        sources: Sequence[SourceRef],
        materials: Sequence[ArtifactRef],
        purpose: ReadPurpose,
    ) -> bool:
        """Every given basis is still releasable for ``purpose`` (also for session checkpoints)."""
        if not all(await self.source_statuses(scope, sources, purpose)):
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
