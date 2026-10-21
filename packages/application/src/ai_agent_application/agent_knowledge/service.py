"""Knowledge base (AW-03): the agent's notes and hypotheses as catalogued files.

Kinds stay distinct: a note is the agent's own text, a hypothesis is an assumption, a method
belongs to the skill library and lived experience belongs to the memory owners, never here.
Dependencies of a note are assigned by the file owner from the attempt's recorded inputs plus
the declared sources; the agent cannot remove them. Search is a plain catalogue/file search.
"""

import builtins
import hashlib
from dataclasses import dataclass
from typing import Literal

from ai_agent_contracts.agent_workplace import FileWrite, ManagedFile
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.agent_workplace.ports import WorkAdmission, WorkspaceFiles

type NoteKind = Literal["note", "hypothesis"]
# What the agent may write here and how its provenance is recorded. Verification of a note
# is never claimed by the writer: it needs a separate confirmed result.
PROVENANCE: dict[str, Literal["generated", "assumption"]] = {
    "note": "generated",
    "hypothesis": "assumption",
}
AREAS = ("knowledge", "inbox", "reference")


@dataclass(frozen=True)
class NoteDraft:
    file_id: str
    path: str
    title: str
    kind: NoteKind
    change_reason: str | None = None
    declared_sources: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class Overview:
    """A short map for the agent's context: counts and titles, never the whole library."""

    counts: dict[str, int]
    recent: tuple[tuple[str, str, str], ...]


class KnowledgeBase:
    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def write(
        self,
        admission: WorkAdmission,
        request_id: str,
        draft: NoteDraft,
        body: str,
        expected_version: int | None,
    ) -> ManagedFile:
        if draft.kind not in PROVENANCE:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, request_id)
        content = body.encode("utf-8")
        return await self.files.write(
            admission,
            FileWrite(
                request_id=request_id,
                file_id=draft.file_id,
                expected_version=expected_version,
                area="knowledge",
                path=draft.path,
                title=draft.title,
                kind=draft.kind,
                provenance=PROVENANCE[draft.kind],
                media_type="text/markdown",
                checksum=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                attempt_id=admission.attempt_id,
                declared_sources=list(draft.declared_sources),
                change_reason=draft.change_reason,
            ),
            content,
        )

    async def search(
        self, scope: Scope, query: str, *, limit: int = 20
    ) -> builtins.list[ManagedFile]:
        rows = await self.files.search(scope, query, limit=limit)
        return [row for row in rows if row.area in AREAS and row.state == "active"]

    async def read(
        self, admission: WorkAdmission, file_id: str, version: int | None = None
    ) -> tuple[ManagedFile, str]:
        row, body = await self.files.read(admission, file_id, version)
        if row.area not in AREAS:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return row, body.decode("utf-8", errors="strict")

    async def overview(self, scope: Scope, *, recent: int = 10) -> Overview:
        counts: dict[str, int] = {}
        latest: builtins.list[ManagedFile] = []
        for name in AREAS:
            rows = [
                row
                for row in await self.files.list(scope, name, limit=200)
                if row.state == "active"
            ]
            counts[name] = len(rows)
            latest += rows
        latest.sort(key=lambda row: row.updated_at, reverse=True)
        return Overview(
            counts=counts,
            recent=tuple((row.id, row.kind, row.title) for row in latest[:recent]),
        )
