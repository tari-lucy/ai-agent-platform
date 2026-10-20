"""Bounded derived private memory, with current source and exact delivery checks.

Selection only ranks what may be shown. Besides the recent windows, older claims/summaries are
reached through the scope's lexical source index: it proposes source ids, and the records citing
them are read from the owner's repository under the same policy. Every shown fragment is still
checked against current source rights/versions and delivery evidence, stays attributed, dated and
bounded, and is data for the model, never an instruction.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ai_agent_application.dialogue.context import (
    CHARS_PER_TOKEN,
    BuiltContext,
    ContextPolicy,
    clip,
    data,
)
from ai_agent_application.dialogue.model import SectionInfo
from ai_agent_application.extraction.evidence import STRENGTH
from ai_agent_application.extraction.relevance import score, words
from ai_agent_application.llm import ChatMessage
from ai_agent_application.memory.recall import estimate_tokens
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import Claim, Source, Summary

from .history import OPERATOR, PrivateHistory

if TYPE_CHECKING:
    from .service import Runtime

SUMMARY_WINDOW = 5  # recent summaries considered, not only the latest
CLAIM_WINDOW = 40  # recent claims considered, not only the last ten
MAX_FRAGMENTS = 3
ARCHIVE_SOURCES = 24  # lexical source hits that may lead to older claims/summaries
ARCHIVE_CLAIMS = 20  # older claims citing those hits, newest first
ARCHIVE_SUMMARIES = 5
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def excerpt(text: str, query: frozenset[str], tokens: int) -> tuple[str, bool]:
    """The head when it fits; else the sentences around the best match, always marked cut.

    A long useful summary is shortened, not dropped, and the part that answers the question is
    preferred over an arbitrary head.
    """
    limit = max(0, tokens) * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text, False
    sentences = _SENTENCE.split(text)
    scores = [score(sentence, query) for sentence in sentences]
    best = scores.index(max(scores)) if scores and max(scores) > 0 else 0
    if best == 0:
        return clip(text, tokens)
    chosen: list[str] = []
    size = 2  # the leading ellipsis
    for sentence in sentences[best:]:
        if size + len(sentence) + 1 > limit:
            break
        chosen.append(sentence)
        size += len(sentence) + 1
    if not chosen:
        return "… " + clip(sentences[best], tokens - 1)[0], True
    return "… " + " ".join(chosen), True


def partners(claim: Claim, pool: dict[str, Claim]) -> list[Claim]:
    """Claims in the pool that this one disagrees with, or that disagree with it."""
    direct = [pool[i] for i in claim.conflict_ids if i in pool]
    reverse = [other for other in pool.values() if claim.id in other.conflict_ids]
    return [*direct, *[c for c in reverse if c not in direct]]


def older_than(window: Sequence[Summary | Claim], size: int) -> dict[str, Any]:
    """Strictly before a full recent window, so the archive limit is not spent on its records."""
    if len(window) < size:
        return {}
    return dict(before=window[-1].created_at, before_id=window[-1].id)


async def archived(
    rt: Runtime,
    scope: Scope,
    query: str,
    summaries: Sequence[Summary],
    claims: Sequence[Claim],
) -> tuple[list[Summary], list[Claim]] | None:
    """Older claims/summaries citing sources that match the question; None if unknown.

    The derived index only proposes source ids (scope, TTL and stem match); the records come from
    the authoritative links with the repository's read policy and are rechecked before showing.
    """
    index = rt.dialogue_index
    if index is None:
        return None
    try:
        found = await index.search(
            scope,
            query,
            None,
            None,
            limit=ARCHIVE_SOURCES,
            now=rt.supervisor.clock.utc(),
            prefix=True,
        )
    except Exception:
        return None
    hits = list(dict.fromkeys(candidate.source_id for candidate in found.lexical))
    if not hits:
        return [], []
    found_summaries = await rt.store.repository(Summary, OPERATOR, scope).recent(
        limit=ARCHIVE_SUMMARIES,
        cites=hits,
        purpose="api_transfer",
        **older_than(summaries, SUMMARY_WINDOW),
    )
    found_claims = await rt.store.repository(Claim, OPERATOR, scope).recent(
        limit=ARCHIVE_CLAIMS, cites=hits, purpose="api_transfer", **older_than(claims, CLAIM_WINDOW)
    )
    seen = {record.id for record in (*summaries, *claims)}
    return (
        [s for s in found_summaries if s.id not in seen],
        [c for c in found_claims if c.id not in seen],
    )


async def include_memory(
    rt: Runtime,
    scope: Scope,
    built: BuiltContext,
    history: PrivateHistory,
    query: str,
    policy: ContextPolicy,
) -> BuiltContext:
    budget = rt.private_api_budget
    if budget is None or not budget.memory_processing:
        return built
    remaining = min(650, max(0, policy.max_tokens - built.manifest.estimated_tokens))
    if remaining < 30:
        return built
    summaries = await rt.store.repository(Summary, OPERATOR, scope).recent(
        limit=SUMMARY_WINDOW, purpose="api_transfer"
    )
    claims = await rt.store.repository(Claim, OPERATOR, scope).recent(
        limit=CLAIM_WINDOW, purpose="api_transfer"
    )
    asked = words(query)
    window = len(summaries)
    older = await archived(rt, scope, query, summaries, claims) if asked else ([], [])
    archive = "unavailable" if older is None else str(len(older[0]) + len(older[1]))
    if older is not None:
        # After the windows: the stable sort keeps newer records first at equal relevance.
        summaries, claims = [*summaries, *older[0]], [*claims, *older[1]]
    pool = {claim.id: claim for claim in claims}
    ranked: list[tuple[int, Summary | Claim]] = []
    for index, summary in enumerate(summaries):
        found = score(summary.content, asked)
        if (index == 0 < window) or found:  # the latest summary stays eligible; others must match
            ranked.append((found, summary))
    for claim in claims:
        found = score(claim.content, asked)
        if found or not asked:
            ranked.append((found, claim))
    # Most relevant first; at equal relevance what a person said outranks the character's own
    # interpretation; then newest (recent() lists newest first and the sort is stable).
    ranked.sort(
        key=lambda row: (
            -row[0],
            -STRENGTH[row[1].knowledge_type] if isinstance(row[1], Claim) else 0,
        )
    )
    ordered: list[Summary | Claim] = []
    for _, record in ranked:
        for member in (record, *(partners(record, pool) if isinstance(record, Claim) else ())):
            if member not in ordered:
                ordered.append(member)
    stats = dict(considered=len(ordered), stale_source=0, over_budget=0, unrelated=0)
    known: dict[str, tuple[SourceRef, ...] | None] = {}

    async def grounded(record: Summary | Claim) -> tuple[SourceRef, ...] | None:
        """Current, deliverable evidence for every source of the record, else None."""
        if record.id not in known:
            grounds: list[SourceRef] = []
            known[record.id] = None
            for ref in record.sources:
                source = await rt.store.repository(Source, OPERATOR, scope).get(
                    ref.source_id, purpose="api_transfer"
                )
                if (
                    ref.scope.partition != scope.partition
                    or source is None
                    or source.version != ref.version
                ):
                    break
                evidence = await history.evidence(scope, source)
                if evidence is None:
                    break
                grounds.extend(evidence)
            else:
                known[record.id] = tuple(grounds)
        return known[record.id]

    fragments, used, truncated = [], 0, False
    for record in ordered:
        grounds = await grounded(record)
        if grounds is None:
            stats["stale_source"] += 1
            continue
        if isinstance(record, Summary):
            label, when, ceiling = "сводка-интерпретация", record.period_end.date(), 350
        else:
            label = "утверждение-" + record.knowledge_type
            when, ceiling = record.created_at.date(), 200
        if record.correction is not None:
            label += "-исправлено-оператором"
            if isinstance(record, Claim):
                label += f"-уверенность-{record.confidence:g}"
            when = record.updated_at.date()
        # Flagged only while the other side is itself deliverable: a revoked claim leaves no trace.
        disputed = ""
        if isinstance(record, Claim):
            for other in partners(record, pool):
                if await grounded(other) is not None:
                    disputed = ' disputed="true"'
                    break
        capacity = min(ceiling, remaining - used)
        if capacity < 45:
            stats["over_budget"] += 1
            continue
        content, cut = excerpt(data(record.content), asked, capacity - 40)
        partial = ' partial="true"' if cut else ""
        fragment = (
            f'<memory by="{label}" at="{when.isoformat()}"{disputed}{partial}>{content}</memory>'
        )
        cost = estimate_tokens("\n" + fragment)
        if cost > remaining - used or not history.remember(grounds):
            stats["over_budget"] += 1
            continue
        fragments.append(fragment)
        truncated = truncated or cut
        used += cost
        if len(fragments) == MAX_FRAGMENTS:
            break
    stats["unrelated"] = len(summaries) + len(claims) - len(ordered)
    # Counts only: no memory text, id or query reaches the log.
    logging.getLogger(__name__).info(
        "Private memory context: shown=%d partial=%d considered=%d stale_source=%d "
        "over_budget=%d unrelated=%d archive=%s",
        len(fragments),
        int(truncated),
        stats["considered"],
        stats["stale_source"],
        stats["over_budget"],
        stats["unrelated"],
        archive,
    )
    if not fragments:
        return built
    last = built.messages[-1]
    return replace(
        built,
        messages=(
            *built.messages[:-1],
            ChatMessage(last.role, last.content + "\n" + "\n".join(fragments)),
        ),
        manifest=replace(
            built.manifest,
            estimated_tokens=built.manifest.estimated_tokens + used,
            sections=(
                *built.manifest.sections,
                SectionInfo(
                    "long_term",
                    len(fragments),
                    len(summaries) + len(claims) - len(fragments),
                    truncated,
                ),
            ),
        ),
    )
