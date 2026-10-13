"""Recall: scope filter -> FTS5 (+ vectors) -> rank fusion -> AUTHORITATIVE recheck -> dedupe.

The index only proposes candidates. Every fragment is re-read through the owner's repository for
the requested purpose (grants, per-record ACL, source rights, TTL, deletion, version) and checked
byte-for-byte against the stored source text before it can enter a context. Lexical search is
the fallback when embeddings are absent or failing; that state is reported, not hidden.
"""

import asyncio
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import (
    Claim,
    RecallFragment,
    RecallRequest,
    RecallResult,
    Source,
)
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.storage import Principal, ReadPurpose, Storage

from .chunking import text_hash
from .health import IndexHealth
from .ports import Candidate, Embedder, RecallIndex, SearchOutcome

PURPOSES: dict[str, ReadPurpose] = {
    "private_context": "private",
    "api_transfer": "api_transfer",
    "public_disclosure": "public_disclosure",
}
RRF_CONSTANT = 60


def estimate_tokens(text: str) -> int:
    """Conservative for Russian text; replaced by the real tokenizer when one is chosen."""
    return math.ceil(len(text) / 3)


class RecallService:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        index: RecallIndex,
        *,
        embedder: Embedder | None = None,
        health: IndexHealth | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        candidates: int = 32,
        embed_timeout: float = 5.0,
        tokens: Callable[[str], int] = estimate_tokens,
    ) -> None:
        if principal.role == "worker" or principal.role not in {"core_owner", "operator"}:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if not 1 <= candidates <= 200:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self.storage, self.principal, self.index, self.embedder = (
            storage,
            principal,
            index,
            embedder,
        )
        self.health = health if health is not None else IndexHealth()
        self.clock, self.candidates, self.embed_timeout, self.tokens = (
            clock,
            candidates,
            embed_timeout,
            tokens,
        )

    @staticmethod
    def _fuse(outcome: SearchOutcome) -> list[Candidate]:
        scores: dict[tuple[str, int], float] = defaultdict(float)
        by_key: dict[tuple[str, int], Candidate] = {}
        for ranking in (outcome.lexical, outcome.vector):
            for rank, item in enumerate(ranking, start=1):
                key = (item.source_id, item.chunk_no)
                scores[key] += 1 / (RRF_CONSTANT + rank)
                by_key[key] = item
        return [by_key[key] for key in sorted(scores, key=lambda k: (-scores[k], k))]

    @staticmethod
    def _attribution(source: Source) -> str:
        who = source.author_id or "автор не установлен"
        trust = (
            f"; уверенность идентификации {source.identity_confidence:.2f}"
            if source.identity_confidence is not None
            else ""
        )
        return (
            f"{source.provenance.value}; {source.source_type}; {who}{trust}; "
            f"{source.occurred_at.date().isoformat()}"
        )

    async def recall(self, request: RecallRequest) -> RecallResult:
        scope, purpose = request.scope, PURPOSES[request.purpose]
        now = self.clock()
        degraded = self.embedder is None
        embedding: list[float] | None = None
        signature: str | None = None
        if self.embedder is not None:
            try:
                async with asyncio.timeout(self.embed_timeout):
                    vectors = await self.embedder.embed([request.query])
                if len(vectors) == 1 and len(vectors[0]) == self.embedder.dimension:
                    embedding, signature = vectors[0], self.embedder.signature
                else:
                    degraded = True
            except Exception:
                degraded = True
        try:
            outcome = await self.index.search(
                scope, request.query, embedding, signature, limit=self.candidates, now=now
            )
        except Exception:
            return RecallResult(
                scope=scope, fragments=[], complete=False, index_state="unavailable"
            )
        if self.embedder is not None and embedding is not None and not outcome.vector_used:
            degraded = True  # index vectors missing / other model: lexical fallback
        if outcome.vector_used and not outcome.vector_complete:
            degraded = True

        repository = self.storage.repository(Source, self.principal, scope)
        cache: dict[str, Source | None] = {}
        fragments: list[RecallFragment] = []
        used_groups: set[str] = set()
        missing: set[str] = set()
        stale = truncated = False
        budget = request.context_token_limit
        for item in self._fuse(outcome):
            if len(fragments) >= request.limit:
                break
            if item.group_key in used_groups:
                continue
            if item.source_id not in cache:
                cache[item.source_id] = await repository.get(item.source_id, purpose=purpose)
            source = cache[item.source_id]
            if source is None:
                missing.add(item.source_id)
                continue
            text = (source.content or "")[item.start : item.end]
            if source.version != item.source_version or text_hash(text) != item.text_hash:
                stale = True
                continue
            cost = self.tokens(text)
            if cost > budget:
                truncated = True
                continue
            budget -= cost
            used_groups.add(item.group_key)
            fragments.append(
                RecallFragment(
                    source=SourceRef(
                        source_id=source.id,
                        version=source.version,
                        scope=scope,
                        availability="available",
                    ),
                    content=text,
                    attribution=self._attribution(source),
                )
            )
        fragments = await self._with_conflicts(request, fragments)
        lagging = stale or bool(self.health.pending(scope))
        state: Literal["ready", "lagging", "degraded"] = (
            "degraded" if degraded else "lagging" if lagging else "ready"
        )
        return RecallResult(
            scope=scope,
            fragments=fragments,
            complete=state == "ready" and not missing and not truncated,
            index_state=state,
            missing_source_ids=sorted(missing)[:200],
        )

    async def _with_conflicts(
        self, request: RecallRequest, fragments: list[RecallFragment]
    ) -> list[RecallFragment]:
        if not fragments:
            return fragments
        try:
            found = await self.index.conflicts_for(
                request.scope, [f.source.source_id for f in fragments]
            )
        except Exception:
            return fragments
        claims = self.storage.repository(Claim, self.principal, request.scope)
        result: list[RecallFragment] = []
        for fragment in fragments:
            visible = []
            for claim_id in found.get(fragment.source.source_id, ()):
                if await claims.get(claim_id) is not None:  # same access rules as any read
                    visible.append(claim_id)
            result.append(fragment.model_copy(update={"conflicts": visible}))
        return result
