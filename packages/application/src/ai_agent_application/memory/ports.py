"""Ports of the derived recall index. The index is rebuildable and never authoritative."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_agent_contracts.common import Scope


@dataclass(frozen=True)
class Chunk:
    """Verbatim slice ``content[start:end]`` of one source."""

    no: int
    start: int
    end: int
    text: str
    text_hash: str


@dataclass(frozen=True)
class IndexedChunk:
    chunk: Chunk
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class IndexedSource:
    source_id: str
    version: int
    expires_at: datetime | None
    chunks: tuple[IndexedChunk, ...]
    group_key: str | None = None


@dataclass(frozen=True)
class ClaimLink:
    claim_id: str
    source_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    source_id: str
    source_version: int
    chunk_no: int
    start: int
    end: int
    text_hash: str
    group_key: str


@dataclass(frozen=True)
class SearchOutcome:
    """Ranked candidates per branch. They are hints; authority is rechecked by the caller."""

    lexical: tuple[Candidate, ...]
    vector: tuple[Candidate, ...]
    vector_used: bool
    vector_complete: bool = True


class Embedder(Protocol):
    @property
    def signature(self) -> str:
        """Model identity + revision + dimension; vectors of different signatures never mix."""
        ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class RecallIndex(Protocol):
    async def put_source(
        self, scope: Scope, entry: IndexedSource, signature: str | None
    ) -> None: ...

    async def remove_source(self, scope: Scope, source_id: str) -> None: ...

    async def set_group(self, scope: Scope, source_ids: Sequence[str], group_key: str) -> None: ...

    async def put_claim(self, scope: Scope, link: ClaimLink) -> None: ...

    async def conflicts_for(
        self, scope: Scope, source_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]: ...

    async def search(
        self,
        scope: Scope,
        query: str,
        embedding: Sequence[float] | None,
        signature: str | None,
        *,
        limit: int,
        now: datetime,
        prefix: bool = False,
    ) -> SearchOutcome:
        """``prefix`` matches word stems (Russian endings); still only candidates."""
        ...

    async def inventory(self, scope: Scope) -> dict[str, int]: ...

    async def replace_scope(
        self,
        scope: Scope,
        sources: Sequence[IndexedSource],
        claims: Sequence[ClaimLink],
        signature: str | None,
    ) -> None:
        """Atomic rebuild of one scope from authoritative data."""
        ...

    async def purge_expired(self, now: datetime) -> int: ...

    async def compact(self) -> None: ...
